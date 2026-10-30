# Optional GitHub Actions CI

The Python and dashboard suites can run locally using the commands in the
README. `github-actions.yml` is an example workflow that runs both suites
on pushes and pull requests, using Python 3.11 and Node 22.

To enable it, add its contents to `.github/workflows/tests.yml` in your
GitHub repository. You can do this in GitHub's web editor, or copy it locally
and push with credentials authorized to manage workflows. GitHub OAuth
credentials need the `workflow` scope for this operation. The example is
stored as documentation so the source repository can be uploaded with
ordinary repository permissions.

The workflow uses pinned revisions of official GitHub Actions and read-only
repository permissions. It needs no Mastodon or Bluesky credentials: tests
supply synthetic configuration and substitute the platform API boundaries.
