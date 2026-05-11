import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CheckCircle2, XCircle, Loader2, Github } from "lucide-react";

export const Route = createFileRoute("/")({
  component: Index,
});

const REQUIRED_FILES = [
  "bot.py",
  "requirements.txt",
  "render.yaml",
  "runtime.txt",
  "fonts/NotoSansBengali-Regular.ttf",
];

type FileStatus = { path: string; exists: boolean };
type CheckResult = {
  isPublic: boolean;
  repoName: string;
  files: FileStatus[];
} | null;

function parseRepo(url: string): { owner: string; repo: string } | null {
  const m = url.trim().match(/github\.com[/:]([^/]+)\/([^/.\s]+)/i);
  if (!m) return null;
  return { owner: m[1], repo: m[2].replace(/\.git$/, "") };
}

function Index() {
  const [url, setUrl] = useState("https://github.com/mhx2n/pdf-artisan-bot");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CheckResult>(null);

  async function check() {
    setError(null);
    setResult(null);
    const parsed = parseRepo(url);
    if (!parsed) {
      setError("সঠিক GitHub URL দিন (যেমন https://github.com/user/repo)");
      return;
    }
    setLoading(true);
    try {
      const repoRes = await fetch(
        `https://api.github.com/repos/${parsed.owner}/${parsed.repo}`,
      );
      if (repoRes.status === 404) {
        setError("রিপো পাওয়া যায়নি — হয় Private অথবা ভুল লিংক");
        setLoading(false);
        return;
      }
      if (!repoRes.ok) {
        setError(`GitHub API error: ${repoRes.status}`);
        setLoading(false);
        return;
      }
      const repoData = await repoRes.json();
      const branch = repoData.default_branch || "main";

      const files = await Promise.all(
        REQUIRED_FILES.map(async (path) => {
          const r = await fetch(
            `https://api.github.com/repos/${parsed.owner}/${parsed.repo}/contents/${path}?ref=${branch}`,
          );
          return { path, exists: r.ok };
        }),
      );

      setResult({
        isPublic: !repoData.private,
        repoName: repoData.full_name,
        files,
      });
    } catch (e) {
      setError("নেটওয়ার্ক সমস্যা: " + (e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  const allOk =
    result && result.isPublic && result.files.every((f) => f.exists);

  return (
    <div className="min-h-screen bg-background p-4 md:p-8">
      <div className="mx-auto max-w-2xl space-y-6">
        <div className="text-center space-y-2">
          <h1 className="text-3xl md:text-4xl font-bold flex items-center justify-center gap-2">
            <Github className="h-8 w-8" />
            GitHub Repo Checker
          </h1>
          <p className="text-muted-foreground">
            আপনার রিপোতে সব প্রয়োজনীয় ফাইল আছে কিনা যাচাই করুন
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>রিপো URL</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-col sm:flex-row gap-2">
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://github.com/user/repo"
                onKeyDown={(e) => e.key === "Enter" && check()}
              />
              <Button onClick={check} disabled={loading}>
                {loading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  "চেক করুন"
                )}
              </Button>
            </div>
            {error && (
              <div className="rounded-md bg-destructive/10 text-destructive p-3 text-sm">
                {error}
              </div>
            )}
          </CardContent>
        </Card>

        {result && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                {allOk ? (
                  <>
                    <CheckCircle2 className="h-6 w-6 text-green-500" />
                    সব ঠিক আছে — Deploy ready!
                  </>
                ) : (
                  <>
                    <XCircle className="h-6 w-6 text-destructive" />
                    কিছু সমস্যা আছে
                  </>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex items-center justify-between border-b pb-2">
                <span className="font-medium">{result.repoName}</span>
                <span
                  className={`text-sm px-2 py-1 rounded ${
                    result.isPublic
                      ? "bg-green-500/10 text-green-600"
                      : "bg-destructive/10 text-destructive"
                  }`}
                >
                  {result.isPublic ? "Public ✓" : "Private ✗"}
                </span>
              </div>
              <ul className="space-y-2">
                {result.files.map((f) => (
                  <li
                    key={f.path}
                    className="flex items-center justify-between text-sm"
                  >
                    <code className="font-mono">{f.path}</code>
                    {f.exists ? (
                      <CheckCircle2 className="h-5 w-5 text-green-500" />
                    ) : (
                      <XCircle className="h-5 w-5 text-destructive" />
                    )}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
