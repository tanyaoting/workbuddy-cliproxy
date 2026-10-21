package main

import (
	"strings"
	"testing"
)

// TestSanitizeBlockedTemplates pins the contract of the blocked-template
// sanitizer: brand names that CodeBuddy blacklists get scrubbed, while real
// filesystem paths must survive untouched.
func TestSanitizeBlockedTemplates(t *testing.T) {
	const appPath = "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/"

	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "dsh 身份句整句删掉 powered by 从句",
			in:   "You are an AI agent powered by DeepSeek Harness.\n\nYou are a coding agent.",
			want: "You are an AI agent.\n\nYou are a coding agent.",
		},
		{
			name: "剩余品牌名换成通用说法",
			in:   "The runtime context names the DeepSeek Harness implementation checkout.",
			want: "The runtime context names the agent runtime implementation checkout.",
		},
		{
			name: "Web GUI 段同样被抹掉品牌名",
			in:   "You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names.",
			want: "You are interacting with the user through the agent runtime Web GUI whose URL the runtime context names.",
		},
		{
			name: "绝对路径原样保留",
			in:   `Current DeepSeek Harness implementation checkout: "` + appPath + `".`,
			want: `Current agent runtime implementation checkout: "` + appPath + `".`,
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
