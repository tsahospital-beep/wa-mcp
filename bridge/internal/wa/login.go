package wa

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"runtime"
	"sync"
	"time"

	"github.com/mdp/qrterminal"
	"go.mau.fi/whatsmeow"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
)

// LoginUI is the login window as seen by the login flow.
type LoginUI interface {
	URL() string
	ShowQR(code string, ttl time.Duration)
	Expired()
	Starting()
	Fail(msg string)
	Connected(phone string)
	ReloadRequests() <-chan struct{}
	SetPair(fn func(ctx context.Context, phone string) (string, error))
	Close()
}

// pairWait is how long a phone-number request waits for a fresh connection.
const pairWait = 15 * time.Second

// loginFlow runs QR rounds until a device is linked.
type loginFlow struct {
	c    *Client
	ui   LoginUI
	wake chan struct{}

	mu        sync.Mutex
	streaming bool // the current QR channel has delivered its first code
}

// Login shows the login window (and a terminal QR code) until the phone links
// this device. A non-empty notice is shown first and the flow waits for the
// user to ask for a code. Only one login flow runs at a time.
func (c *Client) Login(ctx context.Context, notice string) error {
	c.loginMu.Lock()
	defer c.loginMu.Unlock()
	if !c.NeedsLogin() {
		if err := c.claimAccount(ctx); err != nil {
			return err
		}
		return c.cur().Connect()
	}
	var ui LoginUI = terminalUI{}
	if c.opts.NewLoginUI != nil {
		w, err := c.opts.NewLoginUI()
		if err != nil {
			c.opts.Log.Printf("login window unavailable (%v); use the terminal QR code", err)
		} else {
			ui = w
		}
	}
	c.setUI(ui)
	defer c.setUI(nil)
	printLoginURL(c.opts.Terminal, ui.URL(), c.opts.TerminalIsTTY)
	f := &loginFlow{c: c, ui: ui, wake: make(chan struct{}, 1)}
	ui.SetPair(f.pair)
	if notice != "" {
		c.opts.Log.Printf("%s", notice)
		if err := announce(ctx, ui, c.opts.Terminal, c.opts.Enter, notice); err != nil {
			ui.Close()
			return err
		}
	}
	err := f.run(ctx)
	if err != nil {
		ui.Close()
	}
	return err
}

// printLoginURL prints the window address. The one-time token only goes to
// an interactive terminal, never into redirected output such as log files.
func printLoginURL(out io.Writer, link string, tty bool) {
	if link == "" {
		return
	}
	if tty {
		fmt.Fprintf(out, "Login window: %s\n", link)
		return
	}
	if u, err := url.Parse(link); err == nil {
		u.RawQuery = ""
		link = u.String()
	}
	fmt.Fprintf(out, "Login window: %s (the link with its one-time token is not written to redirected output; "+
		"run `wa-bridge login-url` in a terminal to get one)\n", link)
}

// announce shows a notice in the window and waits until the user asks for a code.
func announce(ctx context.Context, ui LoginUI, out io.Writer, enter <-chan struct{}, notice string) error {
	ui.Fail(notice)
	fmt.Fprintln(out, notice+" Press Enter here or use the login window to get a code.")
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-ui.ReloadRequests():
	case <-enter:
	}
	return nil
}

func (f *loginFlow) setStreaming(v bool) {
	f.mu.Lock()
	f.streaming = v
	f.mu.Unlock()
}

func (f *loginFlow) isStreaming() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.streaming && f.c.cur().IsConnected()
}

func (f *loginFlow) poke() {
	select {
	case f.wake <- struct{}{}:
	default:
	}
}

func (f *loginFlow) run(ctx context.Context) error {
	out := f.c.opts.Terminal
	if f.ui.URL() != "" {
		f.c.opts.Log.Printf("login needed: use the login window or scan the QR code below")
	} else {
		f.c.opts.Log.Printf("login needed: scan the QR code below with WhatsApp > Linked devices")
	}
	for {
		wm := f.c.cur()
		if wm.IsConnected() {
			wm.Disconnect()
		}
		f.setStreaming(false)
		f.ui.Starting()
		codes, err := wm.GetQRChannel(ctx)
		if err != nil {
			return fmt.Errorf("login: %w", err)
		}
		if err := wm.Connect(); err != nil {
			f.ui.Fail("Could not reach WhatsApp: " + err.Error())
			f.c.opts.Log.Printf("login: connect: %v", err)
		} else {
			done, err := f.consume(ctx, codes, out)
			if err != nil {
				return err
			}
			if done {
				return f.linked(ctx)
			}
		}
		f.setStreaming(false)
		fmt.Fprintln(out, "Press Enter here or use the login window to get a new code.")
		if err := f.waitReload(ctx); err != nil {
			wm.Disconnect()
			return err
		}
	}
}

// linked checks the account after pairing; a different account than the one
// this store belongs to is unlinked again.
func (f *loginFlow) linked(ctx context.Context) error {
	f.c.mu.Lock()
	devices, err := f.c.container.GetAllDevices(ctx)
	if err == nil && len(devices) == 1 {
		f.c.linked = viewOf(devices[0])
	} else if err == nil {
		err = ErrNotLoggedIn
	}
	f.c.mu.Unlock()
	if err != nil {
		return err
	}
	if err := f.c.claimAccount(ctx); err != nil {
		if errors.Is(err, ErrAccountMismatch) {
			f.ui.Fail("This store belongs to another WhatsApp account. Start the bridge with another store_dir for this account.")
			lctx, cancel := context.WithTimeout(ctx, 15*time.Second)
			if lerr := f.c.cur().Logout(lctx); lerr != nil {
				f.c.opts.Log.Printf("unlink after account mismatch: %v", lerr)
			} else {
				f.c.mu.Lock()
				f.c.linked = sessionView{}
				f.c.mu.Unlock()
			}
			cancel()
		}
		return err
	}
	f.ui.Connected(f.c.OwnJID().User)
	f.c.noteLogin(f.c.Account())
	return nil
}

// consume follows one QR channel until it ends; done means the device is linked.
func (f *loginFlow) consume(ctx context.Context, codes <-chan whatsmeow.QRChannelItem, out io.Writer) (bool, error) {
	for {
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case item, ok := <-codes:
			if !ok {
				f.ui.Expired()
				return false, nil
			}
			switch item.Event {
			case whatsmeow.QRChannelEventCode:
				f.setStreaming(true)
				f.ui.ShowQR(item.Code, item.Timeout)
				fmt.Fprintf(out, "\nScan with WhatsApp > Linked devices > Link a device (valid %s):\n", item.Timeout.Round(time.Second))
				qrterminal.GenerateHalfBlock(item.Code, qrterminal.L, out)
			case whatsmeow.QRChannelSuccess.Event:
				f.setStreaming(false)
				return true, nil
			case whatsmeow.QRChannelTimeout.Event:
				f.setStreaming(false)
				f.ui.Expired()
				f.c.opts.Log.Printf("login code expired")
				return false, nil
			case whatsmeow.QRChannelEventError:
				f.setStreaming(false)
				msg := "The phone did not confirm the link."
				if item.Error != nil {
					msg = item.Error.Error()
				}
				f.ui.Fail(msg)
				f.c.opts.Log.Printf("login error: %s", msg)
				f.c.cur().Disconnect()
				return false, nil
			case whatsmeow.QRChannelEventPasskeyRequest, whatsmeow.QRChannelEventPasskeyResponse:
				f.c.opts.Log.Printf("login: passkey step %q is not supported here; use the QR code", item.Event)
			default:
				f.setStreaming(false)
				f.ui.Fail(loginEventText(item.Event))
				f.c.opts.Log.Printf("login: %s", item.Event)
				f.c.cur().Disconnect()
				return false, nil
			}
		}
	}
}

func loginEventText(event string) string {
	switch event {
	case whatsmeow.QRChannelClientOutdated.Event:
		return "WhatsApp rejected this client version. Update the bridge."
	case whatsmeow.QRChannelScannedWithoutMultidevice.Event:
		return "Turn on multi-device on the phone and try again."
	case whatsmeow.QRChannelErrUnexpectedEvent.Event:
		return "Unexpected state during login. Try again."
	}
	return "Login failed: " + event
}

func (f *loginFlow) waitReload(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-f.wake:
	case <-f.ui.ReloadRequests():
	case <-f.c.opts.Enter:
	}
	return nil
}

// pair requests a phone-number code. When the socket is down it asks the
// flow for a new round and waits for its first QR code first.
func (f *loginFlow) pair(ctx context.Context, phone string) (string, error) {
	if !f.isStreaming() {
		f.poke()
		deadline := time.Now().Add(pairWait)
		for !f.isStreaming() {
			if time.Now().After(deadline) {
				return "", errors.New("no connection to WhatsApp yet, try again in a moment")
			}
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(150 * time.Millisecond):
			}
		}
	}
	wm := f.c.cur()
	code, err := wm.PairPhone(ctx, common.Digits(phone), true, wm.QRClientType, f.c.pairing.browser+" ("+osName()+")")
	return code, common.Upstream("pair phone", err)
}

func osName() string {
	switch runtime.GOOS {
	case "windows":
		return "Windows"
	case "darwin":
		return "Mac OS"
	default:
		return "Linux"
	}
}

// terminalUI is used when no login window can be opened.
type terminalUI struct{}

func (terminalUI) URL() string                                                     { return "" }
func (terminalUI) ShowQR(string, time.Duration)                                    {}
func (terminalUI) Expired()                                                        {}
func (terminalUI) Starting()                                                       {}
func (terminalUI) Fail(string)                                                     {}
func (terminalUI) Connected(string)                                                {}
func (terminalUI) ReloadRequests() <-chan struct{}                                 { return nil }
func (terminalUI) SetPair(func(ctx context.Context, phone string) (string, error)) {}
func (terminalUI) Close()                                                          {}
