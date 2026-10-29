package main

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Package names differ between apt and dnf: vim is vim-enhanced on AlmaLinux.
var (
	aptPackages = []string{"zsh", "git", "ca-certificates", "fzf", "jq", "ripgrep", "tree", "tmux", "vim", "less", "passwd", "util-linux"}
	dnfPackages = []string{"zsh", "git", "ca-certificates", "fzf", "jq", "ripgrep", "tree", "tmux", "vim-enhanced", "less", "passwd", "util-linux", "util-linux-user"}
)

// epelPackages are not in the base AlmaLinux repositories.
var epelPackages = map[string]bool{"fzf": true, "ripgrep": true}

func packagesFor(s system) []string {
	if s.apt {
		return aptPackages
	}
	return dnfPackages
}

func isInstalled(s system, pkg string) bool {
	if s.apt {
		out, _ := exec.Command("dpkg-query", "-W", "-f=${Status}", pkg).Output()
		return string(out) == "install ok installed"
	}
	return exec.Command("rpm", "-q", "--quiet", pkg).Run() == nil
}

// installPackages installs the missing packages with the native package manager.
func installPackages(s system) error {
	var missing []string
	for _, pkg := range packagesFor(s) {
		if !isInstalled(s, pkg) {
			missing = append(missing, pkg)
		}
	}
	if len(missing) == 0 {
		logf("packages: all present")
		return nil
	}
	logf("packages: installing %s", strings.Join(missing, " "))

	if s.apt {
		env := []string{"DEBIAN_FRONTEND=noninteractive"}
		if err := runSystem(env, "apt-get", "update"); err != nil {
			return err
		}
		return runSystem(env, "apt-get", append([]string{"install", "-y", "--no-install-recommends"}, missing...)...)
	}

	if s.el && needsEPEL(missing) && !isInstalled(s, "epel-release") {
		logf("enabling EPEL")
		if err := runSystem(nil, "dnf", "-y", "install", "epel-release"); err != nil {
			return err
		}
	}
	return runSystem(nil, "dnf", append([]string{"-y", "--setopt=install_weak_deps=False", "install"}, missing...)...)
}

func needsEPEL(pkgs []string) bool {
	for _, p := range pkgs {
		if epelPackages[p] {
			return true
		}
	}
	return false
}

// runSystem runs a system command as root, streaming its output to the console.
func runSystem(env []string, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Env = append(os.Environ(), env...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return nil
}
