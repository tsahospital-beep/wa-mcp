//go:build windows

package keystore

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

func platformBackend(dir string) Backend { return dpapiBackend{dir: dir} }

func blob(b []byte) *windows.DataBlob {
	if len(b) == 0 {
		return &windows.DataBlob{}
	}
	return &windows.DataBlob{Size: uint32(len(b)), Data: &b[0]}
}

// take copies a DPAPI output buffer, clears it and frees it.
func take(out *windows.DataBlob) []byte {
	if out.Data == nil {
		return nil
	}
	buf := unsafe.Slice(out.Data, out.Size)
	res := append([]byte(nil), buf...)
	clear(buf)
	windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	return res
}

// protect wraps data for the current user (no CRYPTPROTECT_LOCAL_MACHINE).
func protect(data, entropy []byte) ([]byte, error) {
	name, err := windows.UTF16PtrFromString("wa-mcp")
	if err != nil {
		return nil, err
	}
	var out windows.DataBlob
	if err := windows.CryptProtectData(blob(data), name, blob(entropy), 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out); err != nil {
		return nil, err
	}
	return take(&out), nil
}

func unprotect(data, entropy []byte) ([]byte, error) {
	var out windows.DataBlob
	if err := windows.CryptUnprotectData(blob(data), nil, blob(entropy), 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out); err != nil {
		return nil, err
	}
	return take(&out), nil
}
