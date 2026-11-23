//go:build windows

package fsutil

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	modAdvapi32               = windows.NewLazySystemDLL("advapi32.dll")
	procEncryptFileW          = modAdvapi32.NewProc("EncryptFileW")
	procFileEncryptionStatusW = modAdvapi32.NewProc("FileEncryptionStatusW")
)

func efsStatus(path string) (uint32, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	var st uint32
	if r, _, err := procFileEncryptionStatusW.Call(uintptr(unsafe.Pointer(p)), uintptr(unsafe.Pointer(&st))); r == 0 {
		return 0, err
	}
	return st, nil
}

// efsEncrypt is what "cipher /e" does for one file or folder.
func efsEncrypt(path string) error {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	if r, _, err := procEncryptFileW.Call(uintptr(unsafe.Pointer(p))); r == 0 {
		return err
	}
	return nil
}

func platformEFS() (efsOps, bool) {
	if procEncryptFileW.Find() != nil || procFileEncryptionStatusW.Find() != nil {
		return efsOps{}, false
	}
	return efsOps{status: efsStatus, encrypt: efsEncrypt}, true
}
