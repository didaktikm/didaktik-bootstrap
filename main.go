// Command provisioning sets up zsh, Oh My Zsh and Powerlevel10k for a user and
// for root on Debian/Ubuntu and RHEL-family (AlmaLinux, Rocky Linux, RHEL,
// Fedora) systems. Run it as root: sudo ./provisioning [user].
package main

import (
	"errors"
	"fmt"
	"os"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "dev"

const (
	defaultUser   = "didaktik"
	lockPath      = "/run/lock/provisioning-zsh.lock"
	osReleaseFile = "/etc/os-release"
	systemPath    = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

const usage = `usage: provisioning [--version] [user]

Configures zsh, Oh My Zsh and Powerlevel10k for user (default: didaktik) and root.
Must run as root.

Supported: Debian 11+, Ubuntu 22.04+, AlmaLinux 9+, Rocky Linux 9+, RHEL 9+, Fedora 39+.
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	switch {
	case len(args) == 1 && (args[0] == "-h" || args[0] == "--help"):
		fmt.Print(usage)
		return nil
	case len(args) == 1 && args[0] == "--version":
		fmt.Println(version)
		return nil
	case len(args) > 1:
		return errors.New("too many arguments; " + usage)
	}

	target := defaultUser
	if len(args) == 1 {
		target = args[0]
	}
	if os.Geteuid() != 0 {
		return errors.New("run as root (sudo)")
	}
	if err := os.Setenv("PATH", systemPath); err != nil {
		return err
	}

	unlock, err := acquireLock(lockPath)
	if err != nil {
		return err
	}
	defer unlock()

	primary, err := lookupAccount(target)
	if err != nil {
		return err
	}
	sys, err := detectSystem(osReleaseFile)
	if err != nil {
		return err
	}
	logf("system: %s", sys)

	if err := installPackages(sys); err != nil {
		return err
	}
	if err := setupAccount(primary); err != nil {
		return err
	}
	if primary.Name != "root" {
		root, err := lookupAccount("root")
		if err != nil {
			return err
		}
		if err := setupAccount(root); err != nil {
			return err
		}
	}

	fmt.Println("\nDone. Reconnect to apply the login shell.")
	return nil
}

// acquireLock takes a non-blocking exclusive lock so two runs cannot overlap.
func acquireLock(path string) (func(), error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := lockFile(f); err != nil {
		f.Close()
		return nil, errors.New("another provisioning instance is running")
	}
	return func() { f.Close() }, nil
}
