# Version is stamped into the binary with -X main.version=...
VERSION ?= dev
LDFLAGS := -s -w -X main.version=$(VERSION)
BUILD   := CGO_ENABLED=0 go build -trimpath -ldflags "$(LDFLAGS)"
# GNU coreutils on Linux, perl shasum on macOS.
SHA256 := $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo "shasum -a 256")

.PHONY: test vet fmt-check dist clean

test:
	go test -count=1 ./...

vet:
	go vet ./...

fmt-check:
	@test -z "$$(gofmt -l .)" || { gofmt -l .; echo "run gofmt -w ."; exit 1; }

# Static Linux binaries for x86_64 and arm64, plus SHA256SUMS.
dist: vet test
	mkdir -p dist
	GOOS=linux GOARCH=amd64 $(BUILD) -o dist/provisioning-linux-amd64 .
	GOOS=linux GOARCH=arm64 $(BUILD) -o dist/provisioning-linux-arm64 .
	cd dist && $(SHA256) provisioning-linux-* > SHA256SUMS

clean:
	rm -rf dist
