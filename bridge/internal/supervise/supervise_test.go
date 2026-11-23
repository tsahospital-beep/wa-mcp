package supervise

import (
	"bytes"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

const childEnv = "WA_MCP_SUPERVISE_CHILD"

// TestMain lets the test binary act as a fake bridge when childEnv is set.
func TestMain(m *testing.M) {
	if mode := os.Getenv(childEnv); mode != "" {
		os.Exit(fakeChild(mode))
	}
	os.Exit(m.Run())
}

func fakeChild(mode string) int {
	switch mode {
	case "crash":
		fmt.Println("crashing")
		return 3
	case "fatal":
		fmt.Println("bad config")
		return ExitFatal
	case "serve", "stubborn":
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
		fmt.Printf("ready pid=%d\npartial", os.Getpid())
		for {
			<-sig
			if mode == "serve" {
				fmt.Println(" line")
				fmt.Println("stopping")
				return 0
			}
		}
	case "sleep":
		time.Sleep(time.Minute)
	}
	return 1
}

func child(name, mode string) Child {
	return Child{Name: name, Cmd: func() *exec.Cmd {
		cmd := exec.Command(os.Args[0], "-test.run=^$")
		cmd.Env = append(os.Environ(), childEnv+"="+mode)
		return cmd
	}}
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.String()
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestRestartsWithBackoff(t *testing.T) {
	var out, logs syncBuffer
	s := &Supervisor{
		Children: []Child{child("work", "crash")}, Out: &out, Log: log.New(&logs, "", 0),
		MinBackoff: 40 * time.Millisecond, MaxBackoff: 160 * time.Millisecond,
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	waitFor(t, "four starts", func() bool { return strings.Count(out.String(), "[work] crashing\n") >= 4 })
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	l := logs.String()
	for _, want := range []string{"exited with code 3; starting again in 40ms", "in 80ms", "in 160ms"} {
		if !strings.Contains(l, want) {
			t.Errorf("log lacks %q:\n%s", want, l)
		}
	}
	if strings.Contains(l, "in 320ms") {
		t.Errorf("backoff passed MaxBackoff:\n%s", l)
	}
}

func TestStopsOnlyItsOwnChildren(t *testing.T) {
	// A process the supervisor did not start must survive the shutdown.
	bystander := exec.Command(os.Args[0], "-test.run=^$")
	bystander.Env = append(os.Environ(), childEnv+"=sleep")
	if err := bystander.Start(); err != nil {
		t.Fatal(err)
	}
	gone := make(chan struct{})
	go func() { bystander.Wait(); close(gone) }()
	t.Cleanup(func() { bystander.Process.Kill(); <-gone })

	var out, logs syncBuffer
	s := &Supervisor{
		Children: []Child{child("default", "serve"), child("work", "serve")},
		Out:      &out, Log: log.New(&logs, "", 0), Grace: 10 * time.Second,
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	waitFor(t, "both children", func() bool {
		o := out.String()
		return strings.Contains(o, "[default] ready pid=") && strings.Contains(o, "[work] ready pid=")
	})
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("Run did not return after the context ended")
	}
	o, l := out.String(), logs.String()
	t.Log(l)
	for _, name := range []string{"default", "work"} {
		graceful := strings.Contains(o, "["+name+"] stopping\n") && strings.Contains(l, name+" (pid ")
		killed := strings.Contains(l, "stopping it now") // no console to send the break event to
		if !graceful && !killed {
			t.Errorf("%s was not stopped:\n%s\n%s", name, o, l)
		}
		// the last line without a newline is still written, with its prefix
		if !strings.Contains(o, "["+name+"] partial") {
			t.Errorf("%s: partial line lost:\n%s", name, o)
		}
	}
	select {
	case <-gone:
		t.Fatal("the bystander process was stopped")
	case <-time.After(300 * time.Millisecond):
	}
}

func TestKillsAChildThatIgnoresTheStopRequest(t *testing.T) {
	var out, logs syncBuffer
	s := &Supervisor{Children: []Child{child("club", "stubborn")}, Out: &out, Log: log.New(&logs, "", 0), Grace: 300 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	waitFor(t, "child", func() bool { return strings.Contains(out.String(), "[club] ready") })
	start := time.Now()
	cancel()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("stubborn child was not killed")
	}
	if !strings.Contains(logs.String(), "stopping it now") {
		t.Fatalf("no kill logged:\n%s", logs.String())
	}
	if time.Since(start) > 10*time.Second {
		t.Fatalf("stop took %s", time.Since(start))
	}
}

func TestNothingToRun(t *testing.T) {
	if err := (&Supervisor{}).Run(context.Background()); err == nil {
		t.Fatal("an empty supervisor must fail")
	}
}
