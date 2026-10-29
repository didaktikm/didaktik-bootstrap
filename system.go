package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// system describes the detected distribution and how to manage its packages.
type system struct {
	ID      string
	Version string
	apt     bool // true: Debian/Ubuntu (apt); false: RHEL family (dnf)
	el      bool // true: RHEL-compatible (needs EPEL for fzf and ripgrep)
}

func (s system) String() string {
	pm := "dnf"
	if s.apt {
		pm = "apt"
	}
	return fmt.Sprintf("%s %s (%s)", s.ID, s.Version, pm)
}

// detectSystem reads an os-release file and returns a supported system or an error.
func detectSystem(path string) (system, error) {
	f, err := os.Open(path)
	if err != nil {
		return system{}, err
	}
	defer f.Close()
	return classifySystem(parseOSRelease(f))
}

func parseOSRelease(r io.Reader) map[string]string {
	fields := map[string]string{}
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), "=")
		if !ok {
			continue
		}
		fields[k] = strings.Trim(v, `"'`)
	}
	return fields
}

func classifySystem(fields map[string]string) (system, error) {
	id, version := fields["ID"], fields["VERSION_ID"]
	major := majorVersion(version)
	s := system{ID: id, Version: version}
	switch {
	case id == "debian" && major >= 11, id == "ubuntu" && major >= 22:
		s.apt = true
		return s, nil
	case id == "fedora" && major >= 39:
		return s, nil
	case (id == "almalinux" || id == "rocky" || id == "rhel") && major >= 9:
		s.el = true
		return s, nil
	}
	return system{}, fmt.Errorf("unsupported system: %s %s", id, version)
}

// majorVersion parses "22.04" -> 22 and "9.4" -> 9; unparsable input gives 0.
func majorVersion(v string) int {
	major, _, _ := strings.Cut(v, ".")
	n, err := strconv.Atoi(major)
	if err != nil {
		return 0
	}
	return n
}

// lockFile takes a non-blocking exclusive flock on f.
func lockFile(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
}
