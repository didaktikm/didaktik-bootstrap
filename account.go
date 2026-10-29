package main

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// Account is a local user that the provisioner configures.
type Account struct {
	Name string
	UID  int
	GID  int
	Home string
}

func lookupAccount(name string) (Account, error) {
	u, err := user.Lookup(name)
	if err != nil {
		return Account{}, err
	}
	uid, err := strconv.Atoi(u.Uid)
	if err != nil {
		return Account{}, fmt.Errorf("user %s: bad uid %q", name, u.Uid)
	}
	gid, err := strconv.Atoi(u.Gid)
	if err != nil {
		return Account{}, fmt.Errorf("user %s: bad gid %q", name, u.Gid)
	}
	if !filepath.IsAbs(u.HomeDir) || filepath.Clean(u.HomeDir) == "/" {
		return Account{}, fmt.Errorf("user %s: invalid home %q", name, u.HomeDir)
	}
	if fi, err := os.Stat(u.HomeDir); err != nil || !fi.IsDir() {
		return Account{}, fmt.Errorf("user %s: home is not a directory: %s", name, u.HomeDir)
	}
	return Account{Name: name, UID: uid, GID: gid, Home: u.HomeDir}, nil
}

// userPath is the PATH used for commands run on behalf of an account.
func userPath(home string) string {
	return home + "/.local/bin:/usr/local/bin:/usr/bin:/bin"
}

// commandAs prepares a command that runs as the account, with its own HOME and
// no way to prompt for input (git credentials, sudo, and so on).
func commandAs(ctx context.Context, a Account, name string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = a.Home
	cmd.Env = []string{
		"HOME=" + a.Home,
		"USER=" + a.Name,
		"LOGNAME=" + a.Name,
		"PATH=" + userPath(a.Home),
		"GIT_TERMINAL_PROMPT=0",
	}
	// Empty Groups with NoSetGroups unset drops root's supplementary groups.
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Credential: &syscall.Credential{Uid: uint32(a.UID), Gid: uint32(a.GID)},
	}
	return cmd
}

// runAs runs a command as the account and returns its stdout. On failure the
// error carries stderr, so git and mkdir errors reach the user.
func runAs(a Account, name string, args ...string) (string, error) {
	cmd := commandAs(context.Background(), a, name, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return stdout.String(), fmt.Errorf("%s %s (as %s): %w: %s",
			name, strings.Join(args, " "), a.Name, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

// findExecutable looks name up in a colon-separated PATH and returns its full path, or "".
func findExecutable(path, name string) string {
	for _, dir := range strings.Split(path, ":") {
		p := filepath.Join(dir, name)
		if fi, err := os.Stat(p); err == nil && fi.Mode().IsRegular() && fi.Mode()&0o111 != 0 {
			return p
		}
	}
	return ""
}
