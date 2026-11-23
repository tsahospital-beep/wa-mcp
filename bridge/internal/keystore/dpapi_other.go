//go:build !windows

package keystore

import (
	"fmt"
	"runtime"
)

func platformBackend(dir string) Backend {
	if runtime.GOOS == "darwin" {
		return newKeychain(dir)
	}
	return newSecretService(dir)
}

func protect(data, entropy []byte) ([]byte, error) {
	return nil, fmt.Errorf("%w: DPAPI exists on Windows only", ErrUnavailable)
}

func unprotect(data, entropy []byte) ([]byte, error) {
	return nil, fmt.Errorf("%w: DPAPI exists on Windows only", ErrUnavailable)
}
