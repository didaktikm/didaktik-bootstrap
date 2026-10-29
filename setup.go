package main

import (
	"context"
	"embed"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

//go:embed templates
var templates embed.FS

const shellsFile = "/etc/shells"

func template(name string) []byte {
	data, err := templates.ReadFile("templates/" + name)
	if err != nil {
		panic(err) // embedded at build time: a missing file is a build bug
	}
	return data
}

// setupAccount configures zsh for one account. It is safe to run repeatedly.
func setupAccount(a Account) error {
	logf("configuring %s (%s)", a.Name, a.Home)

	omz := filepath.Join(a.Home, ".oh-my-zsh")
	if err := ensureRepo(a, repo{ohMyZshURL, omz, ohMyZshRev}); err != nil {
		return err
	}
	custom := filepath.Join(omz, "custom")
	cacheDir := filepath.Join(a.Home, ".cache", "zsh", "completions")
	if _, err := runAs(a, "mkdir", "-p", "--",
		filepath.Join(custom, "themes"), filepath.Join(custom, "plugins"),
		filepath.Join(a.Home, ".config", "zsh"), cacheDir); err != nil {
		return err
	}
	for _, r := range pluginRepos(custom) {
		if err := ensureRepo(a, r); err != nil {
			return err
		}
	}
	if err := cacheCompletions(a, cacheDir); err != nil {
		return err
	}

	if err := writeManaged(filepath.Join(a.Home, ".zshrc"), template("zshrc"), a, checkZsh); err != nil {
		return err
	}
	if err := writeManaged(filepath.Join(a.Home, ".config", "zsh", "provisioning-prompt.zsh"),
		template("provisioning-prompt.zsh"), a, checkZsh); err != nil {
		return err
	}
	// Personal overrides are created once and never touched afterwards.
	local := filepath.Join(a.Home, ".config", "zsh", "local.zsh")
	if _, err := checkTarget(local); err != nil {
		return err
	}
	if fi, _ := os.Lstat(local); fi == nil {
		if err := writeAtomic(local, template("local.zsh"), a, checkZsh); err != nil {
			return err
		}
	}

	smokeTest(a)
	return setupShell(a)
}

// checkZsh parse-checks a generated file before it replaces the live one.
func checkZsh(path string) error {
	out, err := exec.Command("zsh", "-n", path).CombinedOutput()
	if err != nil {
		return fmt.Errorf("syntax check of generated %s failed: %w: %s", path, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// cacheCompletions stores kubectl and helm completions as files, so shell
// startup does not run those binaries every time.
func cacheCompletions(a Account, dir string) error {
	for _, name := range []string{"kubectl", "helm"} {
		bin := findExecutable(userPath(a.Home), name)
		if bin == "" {
			continue
		}
		out, err := runAs(a, bin, "completion", "zsh")
		if err != nil {
			warnf("completion for %s skipped: %v", name, err)
			continue
		}
		if err := writeAtomic(filepath.Join(dir, name+".zsh"), []byte(out), a, nil); err != nil {
			return err
		}
	}
	return nil
}

// smokeTest starts an interactive zsh once. A broken config shows up now, not at login.
func smokeTest(a Account) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	cmd := commandAs(ctx, a, "zsh", "-i", "-c", "exit")
	if err := cmd.Run(); err != nil {
		warnf("interactive zsh smoke test failed for %s: %v (check ~/.zshrc)", a.Name, err)
	}
}

// setupShell makes zsh the login shell, adding it to /etc/shells first if needed.
func setupShell(a Account) error {
	zshPath, err := exec.LookPath("zsh")
	if err != nil {
		return fmt.Errorf("zsh is not installed: %w", err)
	}
	if err := ensureShellListed(zshPath); err != nil {
		return err
	}
	current, err := loginShell(a.Name)
	if err != nil {
		return err
	}
	if current == zshPath {
		return nil
	}
	logf("login shell for %s: %s -> %s", a.Name, current, zshPath)
	if out, err := exec.Command("chsh", "-s", zshPath, a.Name).CombinedOutput(); err != nil {
		return fmt.Errorf("chsh %s: %w: %s", a.Name, err, strings.TrimSpace(string(out)))
	}
	return nil
}

func ensureShellListed(path string) error {
	data, err := os.ReadFile(shellsFile)
	if err != nil {
		return err
	}
	lines := strings.Split(string(data), "\n")
	for _, line := range lines {
		if strings.TrimSpace(line) == path {
			return nil
		}
	}
	logf("adding %s to %s", path, shellsFile)
	f, err := os.OpenFile(shellsFile, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	prefix := ""
	if len(data) > 0 && !strings.HasSuffix(string(data), "\n") {
		prefix = "\n"
	}
	_, err = fmt.Fprintf(f, "%s%s\n", prefix, path)
	return err
}

func loginShell(name string) (string, error) {
	data, err := os.ReadFile("/etc/passwd")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) == 7 && fields[0] == name {
			return fields[6], nil
		}
	}
	return "", fmt.Errorf("no passwd entry for %s", name)
}
