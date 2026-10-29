package main

import (
	"fmt"
	"os"
)

func logf(format string, args ...any) {
	fmt.Printf("==> "+format+"\n", args...)
}

func warnf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "WARN: "+format+"\n", args...)
}
