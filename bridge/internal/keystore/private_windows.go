//go:build windows

package keystore

import (
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// checkPrivate refuses a secret file that accounts other than the current
// user and SYSTEM may read.
func checkPrivate(path string) error {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return fmt.Errorf("cannot read the access list of %s: %w", path, err)
	}
	dacl, _, err := sd.DACL()
	if err != nil || dacl == nil {
		return fmt.Errorf("%s has no access list, so everyone may read it; %s", path, privateHint(path))
	}
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err
	}
	system, err := windows.CreateWellKnownSid(windows.WinLocalSystemSid)
	if err != nil {
		return err
	}
	for i := uint16(0); i < dacl.AceCount; i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, uint32(i), &ace); err != nil {
			return err
		}
		if ace.Header.AceType != windows.ACCESS_ALLOWED_ACE_TYPE {
			continue
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		if sid.Equals(user.User.Sid) || sid.Equals(system) {
			continue
		}
		who := sid.String()
		if account, domain, _, err := sid.LookupAccount(""); err == nil {
			who = domain + `\` + account
		}
		return fmt.Errorf("%s can be read by %s; %s", path, who, privateHint(path))
	}
	return nil
}

func privateHint(path string) string {
	return fmt.Sprintf(`limit it to your user and SYSTEM: icacls "%s" /inheritance:r `+
		`/remove:g *S-1-5-32-544 *S-1-3-4 *S-1-5-11 *S-1-5-32-545 *S-1-1-0 /grant:r "%%USERNAME%%:R" SYSTEM:R`, path)
}
