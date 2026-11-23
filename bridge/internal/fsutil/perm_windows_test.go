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
	sid, err := currentUserSID()
	if err != nil {
		t.Fatal(err)
	}
	flags := ""
	if dir {
		flags = "OICI"
	}
	want, err := windows.SecurityDescriptorFromString("D:P(A;" + flags + ";FA;;;" + sid + ")(A;" + flags + ";FA;;;SY)")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(sddl, "D:P") {
		t.Fatalf("%s: DACL not protected: %s", path, sddl)
	}
	_, entries, _ := strings.Cut(sddl, "(")
	_, expected, _ := strings.Cut(want.String(), "(")
	if entries != expected {
		t.Fatalf("%s: unexpected entries: %s", path, sddl)
	}
}
