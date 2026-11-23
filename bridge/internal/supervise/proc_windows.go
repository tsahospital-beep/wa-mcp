//go:build windows

package supervise

import (
	"fmt"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// job holds every child; it is closed only when run-all exits, and its
// kill-on-close limit then ends the children, also when run-all is killed.
var job struct {
	once   sync.Once
	handle windows.Handle
	err    error
}

func jobHandle() (windows.Handle, error) {
	job.once.Do(func() {
		h, err := windows.CreateJobObject(nil, nil)
		if err != nil {
			job.err = err
			return
		}
		info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
		info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		if _, err := windows.SetInformationJobObject(h, windows.JobObjectExtendedLimitInformation,
			uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
			windows.CloseHandle(h)
			job.err = err
			return
		}
		job.handle = h
	})
	return job.handle, job.err
}

// attach puts the child into the job.
func attach(p *os.Process) error {
	h, err := jobHandle()
	if err != nil {
		return fmt.Errorf("job object: %w", err)
	}
	ph, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(p.Pid))
	if err != nil {
		return fmt.Errorf("job object: %w", err)
	}
	defer windows.CloseHandle(ph)
	if err := windows.AssignProcessToJobObject(h, ph); err != nil {
		return fmt.Errorf("job object: %w", err)
	}
	return nil
}

// prepare starts the child in its own process group, so that a break event can
// be sent to that child alone.
func prepare(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.CreationFlags |= windows.CREATE_NEW_PROCESS_GROUP
}

// interrupt sends CTRL_BREAK to the process group of p (its own PID), which
// the bridge handles like Ctrl+C. It fails when this process has no console.
func interrupt(p *os.Process) error {
	return windows.GenerateConsoleCtrlEvent(windows.CTRL_BREAK_EVENT, uint32(p.Pid))
}
