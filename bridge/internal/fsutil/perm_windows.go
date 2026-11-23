//go:build windows

package fsutil

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/windows"
)

func currentUserSID() (string, error) {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return "", err
	}
	return user.User.Sid.String(), nil
}

// setACL replaces the DACL with full access for the current user and SYSTEM
// only and stops inheritance from the parent folder.
func setACL(path string, inherit bool) error {
	sid, err := currentUserSID()
	if err != nil {
		return err
	}
	flags := ""
	if inherit {
		flags = "OICI"
	}
	sddl := fmt.Sprintf("D:P(A;%s;FA;;;%s)(A;%s;FA;;;SY)", flags, sid, flags)
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}

func restrictDir(path string) error  { return setACL(path, true) }
func restrictFile(path string) error { return setACL(path, false) }

// Lock takes an exclusive lock on path (created if missing). The returned
// function releases it.
func Lock(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	ol := new(windows.Overlapped)
	err = windows.LockFileEx(windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, ol)
	if err != nil {
		f.Close()
		return nil, ErrLocked
	}
	return func() {
		windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, ol)
		f.Close()
	}, nil
}

// LinkCount returns the number of hard links of an open file.
func LinkCount(f *os.File) (uint32, error) {
	var info windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(windows.Handle(f.Fd()), &info); err != nil {
		return 0, err
	}
	return info.NumberOfLinks, nil
}

// CheckNoLinks refuses a path when any part of it is a junction (mount
// point) or a symbolic link. Other reparse points, such as cloud-synced
// files, are allowed.
func CheckNoLinks(abs string) error {
	vol := filepath.VolumeName(abs)
	rest := strings.TrimPrefix(abs, vol)
	cur := vol + string(filepath.Separator)
	for _, part := range strings.Split(strings.Trim(rest, `\/`), string(filepath.Separator)) {
		if part == "" {
			continue
		}
		cur = filepath.Join(cur, part)
		name, err := windows.UTF16PtrFromString(cur)
		if err != nil {
			return err
		}
		var data windows.Win32finddata
		h, err := windows.FindFirstFile(name, &data)
		if err != nil {
			return nil // a missing part is reported by the caller's open
		}
		windows.FindClose(h)
		if data.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 &&
			(data.Reserved0 == windows.IO_REPARSE_TAG_MOUNT_POINT || data.Reserved0 == windows.IO_REPARSE_TAG_SYMLINK) {
			return fmt.Errorf("%s is a link or junction", filepath.Base(cur))
		}
	}
	return nil
}
