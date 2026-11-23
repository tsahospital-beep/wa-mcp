//go:build windows

package fsutil

import (
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

// checkPrivate verifies a protected DACL with exactly two entries: the
// current user and SYSTEM.
func checkPrivate(t *testing.T, path string, dir bool) {
	t.Helper()
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	sddl := sd.String()
	sid, _ := currentUserSID()
	if !strings.HasPrefix(sddl, "D:P") {
		t.Fatalf("%s: DACL not protected: %s", path, sddl)
	}
	if strings.Count(sddl, "(") != 2 || !strings.Contains(sddl, sid) || !strings.Contains(sddl, ";;;SY)") {
		t.Fatalf("%s: unexpected entries: %s", path, sddl)
	}
}
