package main

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Pinned revisions. Every repository is checked out at an exact commit, never at
// the moving branch head. To upgrade: change the value, review the upstream diff, rebuild.
const (
	ohMyZshURL = "https://github.com/ohmyzsh/ohmyzsh.git"
	ohMyZshRev = "17a2b4c3a5855524ad4772a17ada0dc474e2fdf3" // no release tags: HEAD at pin time

	p10kURL = "https://github.com/romkatv/powerlevel10k.git"
	p10kRev = "35833ea15f14b71dbcebc7e54c104d8d56ca5268" // v1.20.0

	autosuggestURL = "https://github.com/zsh-users/zsh-autosuggestions.git"
	autosuggestRev = "e52ee8ca55bcc56a17c828767a3f98f22a68d4eb" // v0.7.1

	histSearchURL = "https://github.com/zsh-users/zsh-history-substring-search.git"
	histSearchRev = "400e58a87f72ecec14f783fbd29bc6be4ff1641c" // v1.1.0

	syntaxURL = "https://github.com/zsh-users/zsh-syntax-highlighting.git"
	syntaxRev = "db085e4661f6aafd24e5acb5b2e17e4dd5dddf3e" // 0.8.0
)

type repo struct {
	url string
	dir string
	rev string
}

// pluginRepos lists the repositories that go under the Oh My Zsh custom directory.
func pluginRepos(custom string) []repo {
	return []repo{
		{p10kURL, filepath.Join(custom, "themes", "powerlevel10k"), p10kRev},
		{autosuggestURL, filepath.Join(custom, "plugins", "zsh-autosuggestions"), autosuggestRev},
		{histSearchURL, filepath.Join(custom, "plugins", "zsh-history-substring-search"), histSearchRev},
		{syntaxURL, filepath.Join(custom, "plugins", "zsh-syntax-highlighting"), syntaxRev},
	}
}

// ensureRepo makes dir a git checkout of r.url at exactly r.rev. It creates
// the repository if missing and fetches only the pinned commit, shallowly.
func ensureRepo(a Account, r repo) error {
	dir := r.dir
	if _, err := os.Lstat(dir); errors.Is(err, fs.ErrNotExist) {
		if _, err := runAs(a, "git", "init", "-q", dir); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	if fi, err := os.Stat(filepath.Join(dir, ".git")); err != nil || !fi.IsDir() {
		return fmt.Errorf("not a git checkout: %s", dir)
	}
	if _, err := runAs(a, "git", "-C", dir, "remote", "set-url", "origin", r.url); err != nil {
		if _, err := runAs(a, "git", "-C", dir, "remote", "add", "origin", r.url); err != nil {
			return err
		}
	}
	if headOf(a, dir) == r.rev {
		return nil
	}
	logf("%s: %s", filepath.Base(dir), r.rev[:12])
	if _, err := runAs(a, "git", "-C", dir, "fetch", "-q", "--depth=1", "origin", r.rev); err != nil {
		return err
	}
	if _, err := runAs(a, "git", "-C", dir, "checkout", "-q", "-f", "--detach", r.rev); err != nil {
		return err
	}
	if head := headOf(a, dir); head != r.rev {
		return fmt.Errorf("%s: HEAD is %s, expected %s", dir, head, r.rev)
	}
	return nil
}

func headOf(a Account, dir string) string {
	out, err := runAs(a, "git", "-C", dir, "rev-parse", "-q", "--verify", "HEAD")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}
