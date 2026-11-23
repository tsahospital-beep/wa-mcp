//go:build !windows && !linux

package supervise

import (
	"os"
	"os/exec"
	"syscall"
)

func prepare(*exec.Cmd) {}

func attach(*os.Process) error { return nil }

// interrupt sends SIGTERM to p alone.
func interrupt(p *os.Process) error { return p.Signal(syscall.SIGTERM) }
