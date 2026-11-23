package login

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// openAppWindow opens url as a small app-style window (Edge/Chrome/Brave/
// Chromium --app) with its own temporary profile and returns the process when
// it can be closed later. It never uses the main browser profile: when the
// temporary profile cannot be made or no such browser is found, nothing is
// opened and the address printed in the terminal is the way in.
func openAppWindow(url string, width, height int) (*exec.Cmd, string) {
	profile, err := os.MkdirTemp("", "wa-mcp-login-")
	if err != nil {
		return nil, ""
	}
	args := []string{
		"--app=" + url,
		fmt.Sprintf("--window-size=%d,%d", width, height),
		"--user-data-dir=" + profile,
		"--no-first-run", "--no-default-browser-check", "--disable-extensions",
		"--disable-sync", "--disable-features=Translate,msEdgeSidebarV2",
	}
	for _, bin := range chromiumCandidates() {
		if runtime.GOOS == "darwin" && strings.HasSuffix(bin, ".app") {
			if _, err := os.Stat(bin); err != nil {
				continue
			}
			cmd := exec.Command("open", append([]string{"-na", bin, "--args"}, args...)...)
			if cmd.Start() == nil {
				go cmd.Wait()
				return nil, profile
			}
			continue
		}
		path, err := exec.LookPath(bin)
		if err != nil {
			continue
		}
		cmd := exec.Command(path, args...)
		if cmd.Start() == nil {
			return cmd, profile
		}
	}
	os.RemoveAll(profile)
	return nil, ""
}

func chromiumCandidates() []string {
	switch runtime.GOOS {
	case "windows":
		var out []string
		for _, env := range []string{"ProgramFiles(x86)", "ProgramFiles", "LocalAppData"} {
			base := os.Getenv(env)
			if base == "" {
				continue
			}
			out = append(out,
				filepath.Join(base, `Microsoft\Edge\Application\msedge.exe`),
				filepath.Join(base, `Google\Chrome\Application\chrome.exe`),
				filepath.Join(base, `BraveSoftware\Brave-Browser\Application\brave.exe`),
			)
		}
		return out
	case "darwin":
		return []string{
			"/Applications/Google Chrome.app",
			"/Applications/Microsoft Edge.app",
			"/Applications/Brave Browser.app",
			"/Applications/Chromium.app",
		}
	default:
		return []string{"google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"}
	}
}
