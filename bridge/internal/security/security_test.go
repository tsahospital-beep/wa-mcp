package security

import (
	"bytes"
	"log"
	"strings"
	"testing"
	"time"
)

func TestAlertWindowAndLevels(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	var buf bytes.Buffer
	m := &Monitor{Log: log.New(&buf, "", 0), Now: func() time.Time { return now }}
	m.Record(DeviceRemoved, "the linked device number 3 was removed from this account")
	if m.Alert() {
		t.Fatal("an info event set the alert")
	}
	m.Record(LoggedOut, "WhatsApp unlinked this device")
	if !m.Alert() || !strings.Contains(buf.String(), "warning: security: logged_out") {
		t.Fatalf("warning: %v %q", m.Alert(), buf.String())
	}
	now = now.Add(AlertWindow + time.Minute)
	if m.Alert() {
		t.Fatal("the alert outlived its window")
	}
	if got := m.Events(); len(got) != 2 || got[0].Kind != LoggedOut {
		t.Fatalf("events %+v", got)
	}
	var none *Monitor
	none.Record(LoggedOut, "ignored")
	if len(none.Events()) != 0 || len(none.Protection()) != 0 {
		t.Fatal("a nil monitor returned data")
	}
}
