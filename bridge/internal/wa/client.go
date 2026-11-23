// Package wa wraps the whatsmeow client: session storage, connection and
// login, event ingest, and the network calls the other packages need.
package wa

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	waStore "go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/security"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

var (
	ErrNotConnected    = errors.New("not connected to WhatsApp")
	ErrNotLoggedIn     = errors.New("not logged in to WhatsApp")
	ErrAccountMismatch = errors.New("this store belongs to another WhatsApp account")
)

// ReuploadTimeout is how long we wait for the phone to re-upload expired media.
const ReuploadTimeout = 40 * time.Second

// LoggedOutNotice is shown in the login window after a remote logout.
const LoggedOutNotice = "Account was logged out. Link this device again to continue."

// Options configures a Client.
type Options struct {
	SessionPath  string
	Log          *log.Logger
	LibLogLevel  string // whatsmeow log level: DEBUG, INFO, WARN, ERROR
	RecordCalls  bool
	AutoMarkRead bool
	Emit         func(event string, data any)
	// NewLoginUI opens the login window; nil means terminal only.
	NewLoginUI func() (LoginUI, error)
	// Terminal receives the fallback QR code and the window URL; defaults to stdout.
	Terminal io.Writer
	// Enter fires when Enter is pressed in the terminal (may be nil).
	Enter <-chan struct{}
	// TerminalIsTTY allows printing the one-time login URL; otherwise only
	// the address without its token is printed.
	TerminalIsTTY bool
	// Security receives session events (kinds from package security); may be nil.
	Security func(kind, detail string)
}

// Client owns the session store and the current whatsmeow client. After a
// logout the inner client is replaced, so callers always go through Client.
type Client struct {
	opts      Options
	db        *store.DB
	container *sqlstore.Container
	ingest    *Ingest
	libLog    waLog.Logger
	groups    *groupWorker
	stopWork  context.CancelFunc
	workCtx   context.Context // ends at Close; background work such as the device watch
	watching  atomic.Bool     // the device watch runs
	dirKick   chan struct{}   // asks runDirectory for a full copy
	dir       dirCache

	mu sync.RWMutex
	wm *whatsmeow.Client

	retryMu   sync.Mutex
	retryWait map[string]chan *events.MediaRetry

	loginMu  sync.Mutex
	runCtx   context.Context
	stopping bool
	uiMu     sync.Mutex
	ui       LoginUI // the open login window, if any

	// accepting is false until the account check passed; events are dropped before.
	accepting atomic.Bool
	gate      sync.Mutex
	closing   bool
	inflight  sync.WaitGroup
}

// New opens the session database and prepares a client (it does not connect).
func New(ctx context.Context, db *store.DB, opts Options) (*Client, error) {
	if opts.Log == nil {
		opts.Log = log.New(os.Stdout, "", log.LstdFlags)
	}
	if opts.Terminal == nil {
		opts.Terminal = os.Stdout
	}
	switch opts.LibLogLevel {
	case "DEBUG", "INFO", "WARN", "ERROR":
	default:
		opts.LibLogLevel = "WARN"
	}
	libLog := waLog.Stdout("whatsmeow", opts.LibLogLevel, false)
	// Name and icon shown under Linked devices on the phone.
	waStore.SetOSInfo("WA MCP", [3]uint32{0, 1, 0})
	waStore.DeviceProps.PlatformType = waCompanionReg.DeviceProps_CHROME.Enum()
	dsn := opts.SessionPath + "?_pragma=foreign_keys(1)&_pragma=busy_timeout(8000)&_pragma=journal_mode(WAL)&_pragma=journal_size_limit(" + store.JournalSizeLimit + ")"
	container, err := sqlstore.New(ctx, "sqlite", dsn, libLog.Sub("store"))
	if err != nil {
		return nil, fmt.Errorf("session store: %w", err)
	}
	dev, err := container.GetFirstDevice(ctx)
	if err != nil {
		container.Close()
		return nil, fmt.Errorf("session store: %w", err)
	}
	c := &Client{
		opts:      opts,
		db:        db,
		container: container,
		libLog:    libLog,
		retryWait: map[string]chan *events.MediaRetry{},
		runCtx:    ctx,
	}
	c.ingest = &Ingest{
		DB:          db,
		Title:       c.chatTitle,
		Self:        c.selfIDs,
		AltOf:       c.altOf,
		Emit:        opts.Emit,
		RecordCalls: opts.RecordCalls,
		Log:         opts.Log,
	}
	if opts.AutoMarkRead {
		c.ingest.AutoRead = c.autoRead
	}
	c.groups = newGroupWorker(c.groupInfo, func(ctx context.Context, jid types.JID, info groupInfo) {
		c.ingest.GroupName(ctx, jid, info.Name, false)
		c.ingest.GroupAdmins(ctx, jid, info.Admins)
	})
	c.ingest.GroupAdmin = c.groups.Admin
	workCtx, cancel := context.WithCancel(context.Background())
	c.stopWork = cancel
	c.workCtx = workCtx
	go c.groups.Run(workCtx)
	c.dirKick = make(chan struct{}, 1)
	go c.runDirectory(workCtx)
	c.wm = c.newWM(dev)
	return c, nil
}

func (c *Client) newWM(dev *waStore.Device) *whatsmeow.Client {
	wm := whatsmeow.NewClient(dev, c.libLog.Sub("client"))
	wm.AddEventHandler(c.handle)
	return wm
}

func (c *Client) cur() *whatsmeow.Client {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.wm
}

func (c *Client) emit(event string, data any) {
	if c.opts.Emit != nil {
		c.opts.Emit(event, data)
	}
}

// NeedsLogin reports whether the session has no linked device yet.
func (c *Client) NeedsLogin() bool { return c.cur().Store.ID == nil }

// Start connects, running the login flow first when needed. It blocks until
// connected (or logged in) or ctx ends.
func (c *Client) Start(ctx context.Context) error {
	c.mu.Lock()
	c.runCtx = ctx
	c.mu.Unlock()
	if c.NeedsLogin() {
		return c.Login(ctx, "")
	}
	if err := c.claimAccount(ctx); err != nil {
		return err
	}
	wm := c.cur()
	// Keep retrying in the background when the network is down at start.
	wm.InitialAutoReconnect = true
	return wm.Connect()
}

// claimAccount binds the store to the linked account on first use and
// refuses a different account later.
func (c *Client) claimAccount(ctx context.Context) error {
	own := c.OwnJID()
	if own.IsEmpty() {
		return ErrNotLoggedIn
	}
	stored, err := c.db.AccountJID(ctx)
	if err != nil {
		return err
	}
	if stored == "" {
		if err := c.db.SetAccountJID(ctx, own.String()); err != nil {
			return err
		}
	} else if j, err := types.ParseJID(stored); err != nil || j.User != own.User {
		c.securityEvent(security.AccountMismatch, fmt.Sprintf("the account %s tried to use this store, which belongs to %s",
			common.MaskNumber(own.User), common.MaskNumber(j.User)))
		return fmt.Errorf("%w (%s); the linked account is %s. Use another store_dir for this account",
			ErrAccountMismatch, common.MaskNumber(j.User), common.MaskNumber(own.User))
	}
	c.accepting.Store(true)
	c.kickDirectory() // copy names and the LID map now that the account is known
	return nil
}

// Close stops event handling, waits for running handlers and closes the session store.
func (c *Client) Close() {
	c.gate.Lock()
	c.closing = true
	c.gate.Unlock()
	c.mu.Lock()
	c.stopping = true
	wm := c.wm
	c.mu.Unlock()
	wm.Disconnect()
	common.WaitTimeout(c.inflight.Wait, 10*time.Second)
	c.stopWork()
	select {
	case <-c.groups.Done():
	case <-time.After(5 * time.Second):
	}
	c.container.Close()
}

// Connected reports whether the websocket is up.
func (c *Client) Connected() bool { return c.cur().IsConnected() }

// LoggedIn reports whether the session is authenticated.
func (c *Client) LoggedIn() bool { return c.cur().IsLoggedIn() }

// OwnJID is our phone-number address (empty before login).
func (c *Client) OwnJID() types.JID {
	if id := c.cur().Store.ID; id != nil {
		return id.ToNonAD()
	}
	return types.JID{}
}

// Account returns our masked number for status output.
func (c *Client) Account() string { return common.MaskNumber(c.OwnJID().User) }

func (c *Client) selfIDs() (types.JID, types.JID) {
	st := c.cur().Store
	var pn, lid types.JID
	if st.ID != nil {
		pn = st.ID.ToNonAD()
	}
	if !st.LID.IsEmpty() {
		lid = st.LID.ToNonAD()
	}
	return pn, lid
}

// enter registers a running handler; false once Close has started.
func (c *Client) enter() bool {
	c.gate.Lock()
	defer c.gate.Unlock()
	if c.closing {
		return false
	}
	c.inflight.Add(1)
	return true
}

func (c *Client) handle(evt any) {
	if !c.enter() {
		return
	}
	defer c.inflight.Done()
	ctx := context.Background()
	switch e := evt.(type) {
	case *events.Connected:
		c.opts.Log.Printf("connected to WhatsApp as %s", c.Account())
		c.emit("connection", map[string]any{"state": "connected"})
		c.startDeviceWatch() // devices.go
		return
	case *events.PairSuccess:
		c.securityEvent(security.Paired, fmt.Sprintf("this bridge was linked to %s as a new device", common.MaskNumber(e.ID.User)))
		return
	case *events.Disconnected:
		c.opts.Log.Printf("disconnected from WhatsApp")
		c.emit("connection", map[string]any{"state": "disconnected"})
		return
	case *events.StreamReplaced:
		c.opts.Log.Printf("another client opened this session; this bridge was disconnected")
		c.emit("connection", map[string]any{"state": "replaced"})
		c.securityEvent(security.SessionReplaced, "another client connected with the keys of this session. "+
			"If no second bridge runs with a copy of this store, remove the device on your phone and link again")
		return
	case *events.TemporaryBan:
		c.opts.Log.Printf("WhatsApp reports a temporary ban: %s", e.String())
		c.emit("connection", map[string]any{"state": "temporary_ban", "detail": e.String()})
		return
	case *events.ConnectFailure:
		c.opts.Log.Printf("connect failure: %s", e.Reason.String())
		return
	case *events.LoggedOut:
		c.accepting.Store(false)
		c.opts.Log.Printf("logged out (%s); waiting for a new login", e.Reason.String())
		c.emit("connection", map[string]any{"state": "logged_out"})
		c.securityEvent(security.LoggedOut, fmt.Sprintf("WhatsApp unlinked this device (%s). "+
			"If you did not remove it on your phone, check Linked devices", e.Reason.String()))
		go c.relogin()
		return
	case *events.MediaRetry:
		c.retryMu.Lock()
		ch := c.retryWait[e.MessageID]
		c.retryMu.Unlock()
		if ch != nil {
			select {
			case ch <- e:
			default:
			}
		}
		return
	}
	if !c.accepting.Load() {
		return
	}
	switch e := evt.(type) {
	case *events.Message:
		if _, err := c.ingest.Message(ctx, e, true); err != nil {
			c.opts.Log.Printf("store message %s: %v", e.Info.ID, err)
		}
	case *events.HistorySync:
		n := c.ingest.History(ctx, e.Data, c.cur().ParseWebMessage)
		if n > 0 {
			c.opts.Log.Printf("history sync: stored %d messages", n)
		}
	case *events.Receipt:
		c.ingest.Receipt(ctx, e)
	case *events.CallOffer, *events.CallOfferNotice, *events.CallAccept, *events.CallReject, *events.CallTerminate:
		c.ingest.Call(ctx, evt)
	case *events.GroupInfo:
		if e.Name != nil {
			c.ingest.GroupName(ctx, e.JID, e.Name.Name, true)
		}
	case *events.JoinedGroup:
		c.ingest.GroupName(ctx, e.JID, e.Name, true)
	case *events.Contact:
		if e.Action != nil {
			name := e.Action.GetFullName()
			if name == "" {
				name = e.Action.GetFirstName()
			}
			jids := []types.JID{e.JID}
			for _, alt := range []string{e.Action.GetLidJID(), e.Action.GetPnJID()} {
				if j, err := types.ParseJID(alt); err == nil {
					jids = append(jids, j)
				}
			}
			c.ingest.PersonName(ctx, name, true, jids...)
			c.syncContacts(ctx, jids...) // directory.go
		}
	case *events.PushName:
		c.ingest.PersonName(ctx, e.NewPushName, false, e.JID, e.JIDAlt)
		c.syncContacts(ctx, e.JID, e.JIDAlt)
	case *events.BusinessName:
		c.ingest.PersonName(ctx, e.NewBusinessName, false, e.JID)
		c.syncContacts(ctx, e.JID)
	}
}

// relogin replaces the deleted device with a fresh one and runs the login flow again.
func (c *Client) relogin() {
	c.mu.RLock()
	old, stopping, ctx := c.wm, c.stopping, c.runCtx
	c.mu.RUnlock()
	if stopping {
		return
	}
	// whatsmeow deletes the device store right after the LoggedOut event.
	for i := 0; i < 30 && !old.Store.Deleted; i++ {
		time.Sleep(100 * time.Millisecond)
	}
	old.RemoveEventHandlers()
	old.Disconnect()
	c.mu.Lock()
	c.wm = c.newWM(c.container.NewDevice())
	c.mu.Unlock()
	if err := c.Login(ctx, LoggedOutNotice); err != nil && !errors.Is(err, context.Canceled) {
		c.opts.Log.Printf("login: %v", err)
	}
}

// groupInfo asks WhatsApp for a group's name and admins (used by the background worker).
func (c *Client) groupInfo(ctx context.Context, jid types.JID) (groupInfo, error) {
	wm, err := c.ready()
	if err != nil {
		return groupInfo{}, err
	}
	info, err := wm.GetGroupInfo(ctx, jid)
	if err != nil {
		return groupInfo{}, common.Upstream("group info", err)
	}
	out := groupInfo{Name: info.Name}
	for _, p := range info.Participants {
		if p.IsAdmin || p.IsSuperAdmin {
			for _, j := range []types.JID{p.JID, p.LID, p.PhoneNumber} {
				if !j.IsEmpty() {
					out.Admins = append(out.Admins, j.ToNonAD())
				}
			}
		}
	}
	return out, nil
}

// altOf returns the other address of a user from the session's LID map.
func (c *Client) altOf(ctx context.Context, j types.JID) types.JID {
	lids := c.cur().Store.LIDs
	if lids == nil {
		return types.JID{}
	}
	var alt types.JID
	var err error
	switch j.Server {
	case types.HiddenUserServer:
		alt, err = lids.GetPNForLID(ctx, j)
	case types.DefaultUserServer:
		alt, err = lids.GetLIDForPN(ctx, j)
	}
	if err != nil {
		return types.JID{}
	}
	return alt.ToNonAD()
}

// chatTitle finds a name for a chat that has none stored yet, without network calls.
func (c *Client) chatTitle(ctx context.Context, chat types.JID, push string) string {
	if chat.Server == types.GroupServer {
		c.groups.Request(chat)
		return ""
	}
	if ct, err := c.cur().Store.Contacts.GetContact(ctx, chat); err == nil && ct.Found {
		for _, n := range []string{ct.FullName, ct.FirstName, ct.BusinessName, ct.PushName} {
			if n != "" {
				return n
			}
		}
	}
	return push
}

func (c *Client) autoRead(ctx context.Context, m *store.Message) {
	chat, err1 := types.ParseJID(m.Conv)
	author, err2 := types.ParseJID(m.Author)
	if err1 != nil || err2 != nil {
		return
	}
	now := time.Now()
	if err := c.MarkRead(ctx, []string{m.ID}, now, chat, author); err == nil {
		_ = c.db.MarkRead(ctx, m.Conv, []string{m.ID}, now.Unix())
	}
}

func (c *Client) ready() (*whatsmeow.Client, error) {
	wm := c.cur()
	if wm.Store.ID == nil {
		return nil, ErrNotLoggedIn
	}
	if !wm.IsConnected() {
		return nil, ErrNotConnected
	}
	return wm, nil
}

// SendMessage sends a prepared message.
func (c *Client) SendMessage(ctx context.Context, to types.JID, msg *waE2E.Message) (string, time.Time, error) {
	wm, err := c.ready()
	if err != nil {
		return "", time.Time{}, err
	}
	resp, err := wm.SendMessage(ctx, to, msg)
	if err != nil {
		return "", time.Time{}, common.Upstream("send", err)
	}
	return resp.ID, resp.Timestamp, nil
}

// Upload encrypts and uploads an attachment read from r (streamed through a temporary file).
func (c *Client) Upload(ctx context.Context, r io.Reader, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
	wm, err := c.ready()
	if err != nil {
		return whatsmeow.UploadResponse{}, err
	}
	resp, err := wm.UploadReader(ctx, r, nil, kind)
	return resp, common.Upstream("upload", err)
}

// ChatPresence shows typing, recording or paused in a chat.
func (c *Client) ChatPresence(ctx context.Context, to types.JID, state types.ChatPresence, media types.ChatPresenceMedia) error {
	wm, err := c.ready()
	if err != nil {
		return err
	}
	return common.Upstream("presence", wm.SendChatPresence(ctx, to, state, media))
}

// MarkRead sends read receipts; WhatsApp syncs them to all linked devices.
func (c *Client) MarkRead(ctx context.Context, ids []string, at time.Time, chat, sender types.JID) error {
	wm, err := c.ready()
	if err != nil {
		return err
	}
	return common.Upstream("mark read", wm.MarkRead(ctx, ids, at, chat, sender))
}

// DownloadMediaToFile fetches, decrypts and writes an attachment into file.
func (c *Client) DownloadMediaToFile(ctx context.Context, directPath string, encHash, plainHash, key []byte,
	kind whatsmeow.MediaType, file whatsmeow.File) error {
	wm, err := c.ready()
	if err != nil {
		return err
	}
	return common.Upstream("download", wm.DownloadMediaWithPathToFile(ctx, directPath, encHash, plainHash, key, kind, "", false, file))
}

// RequestReupload asks our phone to upload expired media again and returns the new CDN path.
func (c *Client) RequestReupload(ctx context.Context, m *store.Message) (string, error) {
	wm, err := c.ready()
	if err != nil {
		return "", err
	}
	chat, err := types.ParseJID(m.Conv)
	if err != nil {
		return "", err
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: m.Outgoing, IsGroup: chat.Server == types.GroupServer},
		ID:            m.ID,
	}
	if info.IsGroup && !m.Outgoing && m.Author != "" {
		if sender, err := types.ParseJID(m.Author); err == nil {
			info.Sender = sender
		}
	}
	ch := make(chan *events.MediaRetry, 1)
	c.retryMu.Lock()
	c.retryWait[m.ID] = ch
	c.retryMu.Unlock()
	defer func() {
		c.retryMu.Lock()
		delete(c.retryWait, m.ID)
		c.retryMu.Unlock()
	}()

	if err := wm.SendMediaRetryReceipt(ctx, info, m.MediaKey); err != nil {
		return "", common.Upstream("re-upload request", err)
	}
	timer := time.NewTimer(ReuploadTimeout)
	defer timer.Stop()
	select {
	case evt := <-ch:
		notif, err := whatsmeow.DecryptMediaRetryNotification(evt, m.MediaKey)
		if err != nil {
			return "", common.Upstream("re-upload answer", err)
		}
		if notif.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
			return "", common.Upstream("re-upload", fmt.Errorf("phone could not re-upload the media (%s); it may be deleted on the phone", notif.GetResult()))
		}
		if notif.GetDirectPath() == "" {
			return "", common.Upstream("re-upload", errors.New("phone answered without a media path"))
		}
		return notif.GetDirectPath(), nil
	case <-timer.C:
		return "", common.Upstream("re-upload", fmt.Errorf("phone did not answer within %s; is it online?", ReuploadTimeout))
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// RequestHistory asks our phone for up to count messages older than anchor.
func (c *Client) RequestHistory(ctx context.Context, anchor *store.Message, count int) error {
	wm, err := c.ready()
	if err != nil {
		return err
	}
	chat, err := types.ParseJID(anchor.Conv)
	if err != nil {
		return err
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: anchor.Outgoing, IsGroup: chat.Server == types.GroupServer},
		ID:            anchor.ID,
		Timestamp:     time.Unix(anchor.SentAt, 0),
	}
	_, err = wm.SendPeerMessage(ctx, wm.BuildHistorySyncRequest(info, count))
	return common.Upstream("history request", err)
}

// ProfilePicture returns the URL of the full-size profile picture of jid.
func (c *Client) ProfilePicture(ctx context.Context, jid types.JID) (string, error) {
	wm, err := c.ready()
	if err != nil {
		return "", err
	}
	info, err := wm.GetProfilePictureInfo(ctx, jid, &whatsmeow.GetProfilePictureParams{})
	if err != nil {
		return "", common.Upstream("profile picture", err)
	}
	if info == nil || info.URL == "" {
		return "", nil
	}
	return info.URL, nil
}

// ErrNoLogin is returned by LoginURL when no login is in progress.
var ErrNoLogin = errors.New("no login in progress; the bridge is already linked")

// linker is implemented by login windows that can issue another one-time link.
type linker interface {
	NewLink() (string, error)
}

// LoginURL issues a fresh one-time link to the open login window.
func (c *Client) LoginURL() (string, error) {
	c.uiMu.Lock()
	ui := c.ui
	c.uiMu.Unlock()
	if l, ok := ui.(linker); ok {
		return l.NewLink()
	}
	return "", ErrNoLogin
}

func (c *Client) setUI(ui LoginUI) {
	c.uiMu.Lock()
	c.ui = ui
	c.uiMu.Unlock()
}
