package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func currentOwner() Account {
	return Account{UID: os.Getuid(), GID: os.Getgid()}
}

func TestWriteManagedLifecycle(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".zshrc")
	owner := currentOwner()

	// First write: no backups, the file has the content.
	if err := writeManaged(path, []byte("one\n"), owner, nil); err != nil {
		t.Fatal(err)
	}
	assertEntries(t, dir, ".zshrc")

	// Same content: nothing changes, no backup.
	if err := writeManaged(path, []byte("one\n"), owner, nil); err != nil {
		t.Fatal(err)
	}
	assertEntries(t, dir, ".zshrc")

	// Changed content: original kept once, the replaced version kept as .bak.<time>.
	if err := writeManaged(path, []byte("two\n"), owner, nil); err != nil {
		t.Fatal(err)
	}
	if got := readFile(t, path+beforeSuffix); got != "one\n" {
		t.Fatalf("before-provisioning = %q, want original", got)
	}
	if err := writeManaged(path, []byte("three\n"), owner, nil); err != nil {
		t.Fatal(err)
	}
	if got := readFile(t, path+beforeSuffix); got != "one\n" {
		t.Fatalf("original backup overwritten: %q", got)
	}
	baks, _ := filepath.Glob(path + ".bak.*")
	if len(baks) < 1 {
		t.Fatalf("expected timestamped backups, found none")
	}
	if got := readFile(t, path); got != "three\n" {
		t.Fatalf("target = %q", got)
	}
}

func TestWriteManagedFailedCheckKeepsOldFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".zshrc")
	owner := currentOwner()
	if err := os.WriteFile(path, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	bad := errors.New("syntax")
	err := writeManaged(path, []byte("new\n"), owner, func(string) error { return bad })
	if !errors.Is(err, bad) {
		t.Fatalf("err = %v, want check error", err)
	}
	if got := readFile(t, path); got != "old\n" {
		t.Fatalf("target changed despite failed check: %q", got)
	}
	// Only the target and its backups may exist: no leftover temp files.
	for _, e := range listDir(t, dir) {
		if strings.Contains(e, ".tmp-") {
			t.Fatalf("temp file left behind: %s", e)
		}
	}
}

func TestWriteManagedRefusesSymlink(t *testing.T) {
	dir := t.TempDir()
	real := filepath.Join(dir, "real")
	link := filepath.Join(dir, ".zshrc")
	if err := os.WriteFile(real, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	if err := writeManaged(link, []byte("y"), currentOwner(), nil); err == nil {
		t.Fatal("expected symlink refusal")
	}
	if got := readFile(t, real); got != "x" {
		t.Fatalf("symlink target was modified: %q", got)
	}
}

func TestClassifySystem(t *testing.T) {
	cases := []struct {
		name    string
		fields  map[string]string
		apt, el bool
		wantErr bool
	}{
		{"debian 12", map[string]string{"ID": "debian", "VERSION_ID": "12"}, true, false, false},
		{"ubuntu 24.04", map[string]string{"ID": "ubuntu", "VERSION_ID": "24.04"}, true, false, false},
		{"ubuntu 20.04 too old", map[string]string{"ID": "ubuntu", "VERSION_ID": "20.04"}, false, false, true},
		{"almalinux 9", map[string]string{"ID": "almalinux", "VERSION_ID": "9.4"}, false, true, false},
		{"rocky 10", map[string]string{"ID": "rocky", "VERSION_ID": "10.0"}, false, true, false},
		{"rhel 9", map[string]string{"ID": "rhel", "VERSION_ID": "9.5"}, false, true, false},
		{"fedora 41", map[string]string{"ID": "fedora", "VERSION_ID": "41"}, false, false, false},
		{"fedora 38 too old", map[string]string{"ID": "fedora", "VERSION_ID": "38"}, false, false, true},
		{"centos stream 9 unsupported", map[string]string{"ID": "centos", "VERSION_ID": "9"}, false, false, true},
		{"arch unsupported", map[string]string{"ID": "arch"}, false, false, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s, err := classifySystem(c.fields)
			if (err != nil) != c.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, c.wantErr)
			}
			if err == nil && (s.apt != c.apt || s.el != c.el) {
				t.Fatalf("got apt=%v el=%v, want apt=%v el=%v", s.apt, s.el, c.apt, c.el)
			}
		})
	}
}

func TestParseOSRelease(t *testing.T) {
	in := "NAME=\"Rocky Linux\"\nID=\"rocky\"\nVERSION_ID=\"9.4\"\n# comment\n\nID_LIKE=\"rhel centos fedora\"\n"
	f := parseOSRelease(strings.NewReader(in))
	if f["ID"] != "rocky" || f["VERSION_ID"] != "9.4" || f["NAME"] != "Rocky Linux" {
		t.Fatalf("parsed = %v", f)
	}
}

func assertEntries(t *testing.T, dir string, want ...string) {
	t.Helper()
	got := listDir(t, dir)
	if len(got) != len(want) {
		t.Fatalf("entries = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("entries = %v, want %v", got, want)
		}
	}
}

func listDir(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !strings.Contains(e.Name(), ".bak.") && !strings.HasSuffix(e.Name(), beforeSuffix) {
			names = append(names, e.Name())
		}
	}
	return names
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
