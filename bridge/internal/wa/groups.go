package wa

import (
	"context"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
)

// groupInfo is what the bridge keeps about a group: its name and admins.
type groupInfo struct {
	Name   string
	Admins []types.JID // phone-number and LID addresses
	At     time.Time
}

func (g groupInfo) isAdmin(user types.JID) bool {
	for _, a := range g.Admins {
		if sameAddress(a, user) {
			return true
		}
	}
	return false
}

// groupWorker looks up group names and admins in the background so the event
// handler never waits for the network. Results are cached; failed lookups
// are not retried for a while.
type groupWorker struct {
	fetch   func(ctx context.Context, jid types.JID) (groupInfo, error)
	apply   func(ctx context.Context, jid types.JID, info groupInfo)
	ttl     time.Duration // how long a cached member list counts as known
	retry   time.Duration
	timeout time.Duration
	now     func() time.Time

	queue  chan types.JID
	mu     sync.Mutex
	queued map[types.JID]bool
	failed map[types.JID]time.Time
	cache  map[types.JID]groupInfo
	done   chan struct{}

	// fetched is called after each lookup has been recorded (tests use it). May be nil.
	fetched func(jid types.JID, err error)
}

func newGroupWorker(fetch func(ctx context.Context, jid types.JID) (groupInfo, error),
	apply func(ctx context.Context, jid types.JID, info groupInfo)) *groupWorker {
	return &groupWorker{
		fetch: fetch, apply: apply,
		ttl: time.Hour, retry: 30 * time.Minute, timeout: 15 * time.Second, now: time.Now,
		queue:  make(chan types.JID, 256),
		queued: map[types.JID]bool{},
		failed: map[types.JID]time.Time{},
		cache:  map[types.JID]groupInfo{},
		done:   make(chan struct{}),
	}
}

// Request asks for a lookup without blocking. It reports whether one was
// queued (false when already queued, failed recently, or the queue is full).
func (w *groupWorker) Request(jid types.JID) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if info, ok := w.cache[jid]; ok && w.now().Sub(info.At) < w.ttl {
		return false
	}
	if w.queued[jid] {
		return false
	}
	if until, ok := w.failed[jid]; ok && w.now().Before(until) {
		return false
	}
	select {
	case w.queue <- jid:
		w.queued[jid] = true
		return true
	default:
		return false
	}
}

// Admin answers from the cache; known is false (and a lookup is requested)
// when the group's member list is missing or stale.
func (w *groupWorker) Admin(group, user types.JID) (admin, known bool) {
	w.mu.Lock()
	info, ok := w.cache[group]
	fresh := ok && w.now().Sub(info.At) < w.ttl
	w.mu.Unlock()
	if !fresh {
		w.Request(group)
		return false, false
	}
	return info.isAdmin(user), true
}

// Run serves requests until ctx ends.
func (w *groupWorker) Run(ctx context.Context) {
	defer close(w.done)
	for {
		select {
		case <-ctx.Done():
			return
		case jid := <-w.queue:
			lctx, cancel := context.WithTimeout(ctx, w.timeout)
			info, err := w.fetch(lctx, jid)
			cancel()
			w.mu.Lock()
			delete(w.queued, jid)
			if err != nil {
				w.failed[jid] = w.now().Add(w.retry)
			} else {
				delete(w.failed, jid)
				info.At = w.now()
				w.cache[jid] = info
			}
			w.mu.Unlock()
			if err == nil {
				w.apply(ctx, jid, info)
			}
			if w.fetched != nil {
				w.fetched(jid, err)
			}
		}
	}
}

// Done is closed when Run has returned.
func (w *groupWorker) Done() <-chan struct{} { return w.done }
