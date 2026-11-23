package keystore

import "sync"

// Memory is a Backend that keeps items in memory only (for tests).
type Memory struct {
	mu    sync.Mutex
	items map[string][]byte
	// Err, when set, is returned by every call (an unusable keystore).
	Err error
}

// NewMemory returns an empty in-memory backend.
func NewMemory() *Memory { return &Memory{items: map[string][]byte{}} }

func (m *Memory) Name() string                { return "memory" }
func (m *Memory) Describe(item string) string { return "memory (" + item + ")" }

func (m *Memory) Get(item string) ([]byte, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.Err != nil {
		return nil, m.Err
	}
	v, ok := m.items[item]
	if !ok {
		return nil, ErrNotFound
	}
	return append([]byte(nil), v...), nil
}

func (m *Memory) Put(item string, secret []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.Err != nil {
		return m.Err
	}
	m.items[item] = append([]byte(nil), secret...)
	return nil
}

func (m *Memory) Delete(item string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.Err != nil {
		return m.Err
	}
	delete(m.items, item)
	return nil
}
