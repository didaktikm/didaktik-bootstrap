package main

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

const beforeSuffix = ".before-provisioning"

// checkTarget refuses symlinks and non-regular files. It returns nil info when
// the path does not exist yet.
func checkTarget(path string) (fs.FileInfo, error) {
	fi, err := os.Lstat(path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return nil, nil
	case err != nil:
		return nil, err
	case fi.Mode()&fs.ModeSymlink != 0:
		return nil, fmt.Errorf("refusing symlink: %s", path)
	case !fi.Mode().IsRegular():
		return nil, fmt.Errorf("not a regular file: %s", path)
	}
	return fi, nil
}

// writeManaged replaces a provisioner-owned file. Unchanged content is a no-op.
// A changed file is backed up first: the original once as *.before-provisioning,
// and every replaced version as *.bak.<UTC time>.
func writeManaged(path string, content []byte, owner Account, check func(string) error) error {
	fi, err := checkTarget(path)
	if err != nil {
		return err
	}
	if fi != nil {
		current, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if bytes.Equal(current, content) {
			return nil
		}
		if err := backup(path, current, fi.Mode().Perm(), owner); err != nil {
			return err
		}
	}
	return writeAtomic(path, content, owner, check)
}

func backup(path string, data []byte, mode fs.FileMode, owner Account) error {
	orig := path + beforeSuffix
	if _, err := os.Lstat(orig); errors.Is(err, fs.ErrNotExist) {
		if err := writeNewFile(orig, data, mode, owner); err != nil {
			return err
		}
	}
	// Nanosecond precision: two replacements in the same second must not collide.
	stamp := time.Now().UTC().Format("20060102T150405.000000000Z")
	return writeNewFile(path+".bak."+stamp, data, mode, owner)
}

// writeNewFile creates a file that must not exist yet, so it never overwrites a backup.
func writeNewFile(path string, data []byte, mode fs.FileMode, owner Account) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Lchown(path, owner.UID, owner.GID)
}

// writeAtomic writes a temporary file next to path, checks it, then renames it
// over path. Readers never see a partial file, and a failed check leaves the old one.
func writeAtomic(path string, content []byte, owner Account, check func(string) error) error {
	if _, err := checkTarget(path); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name()) // harmless after a successful rename

	if _, err := tmp.Write(content); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return err
	}
	if err := os.Lchown(tmp.Name(), owner.UID, owner.GID); err != nil {
		return err
	}
	if check != nil {
		if err := check(tmp.Name()); err != nil {
			return err
		}
	}
	return os.Rename(tmp.Name(), path)
}
