package wa

import (
	"context"
	"errors"
	"log"
	"maps"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/types/events"
)

type appStateSync struct {
	ctx       context.Context
	cancel    context.CancelFunc
	mu        sync.Mutex
	wg        sync.WaitGroup
	state     map[string]string
	attempted map[string]bool
	delay     time.Duration
}

func newAppStateSync(parent context.Context) *appStateSync {
	ctx, cancel := context.WithCancel(parent)
	return &appStateSync{ctx: ctx, cancel: cancel, state: map[string]string{}, attempted: map[string]bool{}, delay: time.Second}
}

func (s *appStateSync) complete(name appstate.WAPatchName) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state[string(name)] = "synced"
}

type fetchState func(context.Context, appstate.WAPatchName, bool, bool) error

func (s *appStateSync) onError(e *events.AppStateSyncError, fetch fetchState, logger *log.Logger) {
	s.mu.Lock()
	defer s.mu.Unlock()
	name := string(e.Name)
	if s.ctx.Err() != nil || s.state[name] == "recovering" {
		return
	}
	s.state[name] = "failed"
	verify := errors.Is(e.Error, appstate.ErrMismatchingLTHash) || errors.Is(e.Error, appstate.ErrMismatchingPatchMAC) ||
		errors.Is(e.Error, appstate.ErrMismatchingContentMAC) || errors.Is(e.Error, appstate.ErrMismatchingIndexMAC)
	if !verify || s.attempted[name] {
		return
	}
	s.attempted[name] = true
	s.state[name] = "recovering"
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		var err error
		for attempt := 0; attempt < 3; attempt++ {
			select {
			case <-s.ctx.Done():
				return
			case <-time.After(s.delay << attempt):
			}
			// A full fetch resets the stored version under whatsmeow's sync lock.
			ctx, cancel := context.WithTimeout(s.ctx, time.Minute)
			err = fetch(ctx, e.Name, true, false)
			cancel()
			if err == nil {
				break
			}
		}
		if s.ctx.Err() != nil {
			return
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if err != nil {
			s.state[name] = "failed"
			logger.Printf("app state %s recovery failed: %v", name, err)
		} else {
			s.state[name] = "synced"
			logger.Printf("app state %s recovered", name)
		}
	}()
}

func (s *appStateSync) stop() {
	// Synchronize with any handler registering a worker before cancellation.
	s.mu.Lock()
	s.cancel()
	s.mu.Unlock()
	s.wg.Wait()
}

func (c *Client) AppStateStatus() map[string]string {
	c.mu.RLock()
	s := c.app
	c.mu.RUnlock()
	s.mu.Lock()
	defer s.mu.Unlock()
	return maps.Clone(s.state)
}
