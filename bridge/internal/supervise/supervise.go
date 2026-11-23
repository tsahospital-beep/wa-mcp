// Package supervise runs one child process per account and starts it again
// when it stops. Children are stopped through their own process IDs only.
package supervise

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os/exec"
	"sync"
	"time"
)

// Child is one supervised program. Cmd builds a fresh command for every start.
type Child struct {
	Name string
	Cmd  func() *exec.Cmd
}

// Supervisor keeps its children running until the context ends.
type Supervisor struct {
	Children []Child
	Out      io.Writer   // child output; every line starts with "[name] "
	Log      *log.Logger // supervisor messages; nil = none

	MinBackoff  time.Duration // first wait before a restart (default 1 s)
	MaxBackoff  time.Duration // longest wait (default 60 s)
	StableAfter time.Duration // a child that ran this long restarts after MinBackoff again (default 60 s)
	Grace       time.Duration // how long a child may take to stop before it is killed (default 30 s)

	mu sync.Mutex // serializes writes to Out
}

func (s *Supervisor) logf(format string, args ...any) {
	if s.Log != nil {
		s.Log.Printf(format, args...)
	}
}

func (s *Supervisor) defaults() {
	if s.MinBackoff <= 0 {
		s.MinBackoff = time.Second
	}
	if s.MaxBackoff < s.MinBackoff {
		s.MaxBackoff = max(60*time.Second, s.MinBackoff)
	}
	if s.StableAfter <= 0 {
		s.StableAfter = 60 * time.Second
	}
	if s.Grace <= 0 {
		s.Grace = 30 * time.Second
	}
	if s.Out == nil {
		s.Out = io.Discard
	}
}

// Run starts every child and restarts children that exit, with a growing
// pause. When ctx ends it asks each running child to stop, kills a child that
// is still running after Grace, and returns once all of them have exited.
func (s *Supervisor) Run(ctx context.Context) error {
	if len(s.Children) == 0 {
		return errors.New("nothing to run")
	}
	s.defaults()
	var wg sync.WaitGroup
	for _, c := range s.Children {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.keep(ctx, c)
		}()
	}
	wg.Wait()
	return nil
}

func (s *Supervisor) keep(ctx context.Context, c Child) {
	backoff := s.MinBackoff
	for ctx.Err() == nil {
		started := time.Now()
		code, err := s.runOnce(ctx, c)
		if ctx.Err() != nil {
			return
		}
		if time.Since(started) >= s.StableAfter {
			backoff = s.MinBackoff
		}
		if err == nil && code == ExitFatal {
			s.logf("run-all: %s stopped with a configuration or key error (exit code %d); it is not started again. "+
				"Fix the problem in its log and start run-all again", c.Name, code)
			return
		}
		if err != nil {
			s.logf("run-all: %s: %v; starting again in %s", c.Name, err, backoff)
		} else {
			s.logf("run-all: %s exited with code %d; starting again in %s", c.Name, code, backoff)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		backoff = min(backoff*2, s.MaxBackoff)
	}
}

// runOnce starts the child and waits for it, or stops it when ctx ends.
func (s *Supervisor) runOnce(ctx context.Context, c Child) (int, error) {
	cmd := c.Cmd()
	prepare(cmd)
	out := &lineWriter{prefix: []byte("[" + c.Name + "] "), s: s}
	cmd.Stdout, cmd.Stderr = out, out
	if err := cmd.Start(); err != nil {
		return -1, err
	}
	pid := cmd.Process.Pid
	if err := attach(cmd.Process); err != nil {
		s.logf("run-all: %s (pid %d): %v; it may outlive run-all if run-all is killed", c.Name, pid, err)
	}
	s.logf("run-all: %s started (pid %d)", c.Name, pid)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	defer out.flush()
	select {
	case err := <-done:
		return exitCode(err), nil
	case <-ctx.Done():
	}
	if err := interrupt(cmd.Process); err != nil {
		s.logf("run-all: %s (pid %d): %v; stopping it now", c.Name, pid, err)
		_ = cmd.Process.Kill()
		<-done
		return -1, nil
	}
	select {
	case <-done:
		s.logf("run-all: %s (pid %d) stopped", c.Name, pid)
	case <-time.After(s.Grace):
		s.logf("run-all: %s (pid %d) did not stop within %s; stopping it now", c.Name, pid, s.Grace)
		_ = cmd.Process.Kill()
		<-done
	}
	return 0, nil
}

func exitCode(err error) int {
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		return ee.ExitCode()
	}
	if err != nil {
		return -1
	}
	return 0
}

// lineWriter prefixes every line of one child and writes whole lines only, so
// the output of two children never mixes inside a line.
type lineWriter struct {
	prefix []byte
	s      *Supervisor
	buf    []byte
}

func (w *lineWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)
	for {
		i := bytes.IndexByte(w.buf, '\n')
		if i < 0 {
			break
		}
		w.emit(w.buf[:i+1])
		w.buf = w.buf[i+1:]
	}
	if len(w.buf) > 64<<10 {
		w.flush()
	}
	return len(p), nil
}

func (w *lineWriter) flush() {
	if len(w.buf) > 0 {
		w.emit(append(w.buf, '\n'))
		w.buf = nil
	}
}

func (w *lineWriter) emit(line []byte) {
	w.s.mu.Lock()
	defer w.s.mu.Unlock()
	fmt.Fprintf(w.s.Out, "%s%s", w.prefix, line)
}
