//go:build linux

package supervise

import (
	"os"
	"os/exec"
	"syscall"
)

// prepare makes the kernel send SIGTERM to the child when run-all dies, so a
// killed supervisor leaves no bridge running.
func prepare(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Pdeathsig = syscall.SIGTERM
}

func attach(*os.Process) error { return nil }

// interrupt sends SIGTERM to p alone.
func interrupt(p *os.Process) error { return p.Signal(syscall.SIGTERM) }
