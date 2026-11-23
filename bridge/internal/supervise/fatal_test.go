package supervise

import (
	"context"
	"log"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestFatalExitIsNotRestarted(t *testing.T) {
	var out, logs syncBuffer
	s := &Supervisor{Children: []Child{child("work", "fatal"), child("club", "crash")}, Out: &out,
		Log: log.New(&logs, "", 0), MinBackoff: 20 * time.Millisecond, MaxBackoff: 40 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	waitFor(t, "the other child restarting", func() bool { return strings.Count(out.String(), "[club] crashing\n") >= 3 })
	cancel()
	<-done
	if n := strings.Count(out.String(), "[work] bad config\n"); n != 1 {
		t.Fatalf("the child with a fatal error started %d times", n)
	}
	if !strings.Contains(logs.String(), "is not started again") {
		t.Fatalf("log: %s", logs.String())
	}
}

func TestChildIsAttachedForCleanup(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), childEnv+"=sleep")
	prepare(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cmd.Process.Kill(); cmd.Wait() }()
	if err := attach(cmd.Process); err != nil {
		t.Fatalf("attach: %v", err)
	}
}
