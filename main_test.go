package main

import (
	"strings"
	"testing"
)

// TestSanitizeBlockedTemplates pins the contract of the blocked-template
// sanitizer: templates CodeBuddy blacklists get rewritten without changing what
// the model is told, while real filesystem paths and unrelated wording survive.
func TestSanitizeBlockedTemplates(t *testing.T) {
	const appPath = "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/"

	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "dsh 身份句去掉 powered by 从句",
			in:   "You are an AI agent powered by DeepSeek Harness.\n\nYou are a coding agent.",
			want: "You are an AI assistant.\n\nYou are a coding agent.",
		},
		{
			name: "剩余品牌名换成通用说法，runtime context 一并通用化",
			in:   "The runtime context names the DeepSeek Harness implementation checkout.",
			want: "The context names the agent implementation checkout.",
		},
		{
			name: "Web GUI 段同样被抹掉品牌名",
			in:   "You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names.",
			want: "You are interacting with the user through the agent Web GUI whose URL the context names.",
		},
		{
			name: "绝对路径原样保留",
			in:   `Current DeepSeek Harness implementation checkout: "` + appPath + `".`,
			want: `Current agent implementation checkout: "` + appPath + `".`,
		},
		{
			name: "Claude Code 身份句改写",
			in:   "You are Claude Code, Anthropic's official CLI for Claude.",
			want: "You are Claude Code, Anthropic's official CLI tool for Claude.",
		},
		{
			name: "git 分支注入改写",
			in:   "Main branch (you will usually use this for PRs)",
			want: "Default branch (you will usually use this for PRs)",
		},
		{
			name: "@file 引用说明里的 workspace root 改写",
			in:   "Tokens prefixed with @ are paths the user explicitly referenced. Relative paths resolve from the workspace root; absolute paths identify files or directories on the host.",
			want: "Tokens prefixed with @ are paths the user explicitly referenced. Relative paths resolve from the project root; absolute paths identify files or directories on the host.",
		},
		{
			name: "沙箱策略名 workspace-write 不受影响",
			in:   "Current DSH file policy: workspace-write. Instructions from: AGENTS.md",
			want: "Current DSH file policy: workspace-write. Instructions from: AGENTS.md",
		},
		{
			name: "无关文本原样返回",
			in:   "hello world",
			want: "hello world",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := sanitizeBlockedTemplates(tc.in)
			if got != tc.want {
				t.Fatalf("sanitizeBlockedTemplates()\n got: %q\nwant: %q", got, tc.want)
			}
		})
	}
}

// TestSanitizeIdempotent guards the rewriter used on every message part: a
// second pass must be a no-op, or text echoed back into history would drift
// further on each request.
func TestSanitizeIdempotent(t *testing.T) {
	in := strings.Join([]string{
		"You are an AI agent powered by DeepSeek Harness.",
		"Relative paths resolve from the workspace root; absolute paths identify files or directories on the host.",
		`Checkout: "` + "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/" + `".`,
		"You are Claude Code, Anthropic's official CLI for Claude.",
	}, "\n\n")

	once := sanitizeBlockedTemplates(in)
	twice := sanitizeBlockedTemplates(once)
	if once != twice {
		t.Fatalf("sanitizer is not idempotent:\n once: %q\ntwice: %q", once, twice)
	}
}

// TestSanitizeLeavesNoBrandName guards the actual goal: after sanitizing a
// realistic dsh prompt, no "DeepSeek Harness" prose remains for an upstream
// fingerprint to match on — except inside the preserved .app path.
func TestSanitizeLeavesNoBrandName(t *testing.T) {
	prompt := strings.Join([]string{
		"You are an AI agent powered by DeepSeek Harness.",
		"You are a coding agent powered by the deepseek-v4.1-flash model.",
		"The runtime context names the DeepSeek Harness implementation checkout.",
		`The checkout location is "` + "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/" + `".`,
		`You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names.`,
		`When the user refers to "this page", "this GUI", or "this app" without naming another target, they mean this GUI.`,
	}, "\n\n")

	got := sanitizeBlockedTemplates(prompt)

	if strings.Contains(got, "DeepSeek Harness Web GUI") {
		t.Fatalf("brand name still present in GUI section: %q", got)
	}
	if strings.Contains(got, "powered by DeepSeek Harness") {
		t.Fatalf("identity clause not removed: %q", got)
	}
	// 只剩路径里那一次
	if n := strings.Count(got, "DeepSeek Harness"); n != 1 {
		t.Fatalf("expected exactly 1 remaining occurrence (the .app path), got %d:\n%s", n, got)
	}
	if !strings.Contains(got, "/Applications/DeepSeek Harness.app/") {
		t.Fatalf(".app path was damaged: %q", got)
	}
	if strings.Contains(got, dshAppPathSentinel) {
		t.Fatalf("sentinel leaked into output: %q", got)
	}
}

// TestSanitizeClearsFileReferenceFingerprint covers the failure found against
// the dsh desktop surface: the @file guidance's first two sentences are
// rejected upstream only when they appear together, and "workspace root" is the
// smallest edit that clears it.
func TestSanitizeClearsFileReferenceFingerprint(t *testing.T) {
	const guidance = "Tokens prefixed with @ are paths the user explicitly referenced. " +
		"Relative paths resolve from the workspace root; absolute paths identify files or directories on the host. " +
		"A trailing slash marks a directory: list its contents when they matter."

	got := sanitizeBlockedTemplates(guidance)
	if strings.Contains(got, "workspace root") {
		t.Fatalf("fingerprint phrase survived: %q", got)
	}
	if !strings.Contains(got, "project root") {
		t.Fatalf("replacement missing: %q", got)
	}
	if !strings.Contains(got, "absolute paths identify files or directories on the host") {
		t.Fatalf("surrounding guidance was damaged: %q", got)
	}
}
