# Repository workflow

- Never push directly to `main`. All changes must go through a pull request.
- Before editing tracked files, create or switch to a feature branch. If changes
  already exist on `main`, move them to a feature branch before committing.
- Treat requests to "commit and push" as authorization to commit and push a
  feature branch and open a PR targeting `main`, never as permission to push `main`.
- Before every push, verify the current branch and the explicit push destination.
  Push the feature branch by name; do not use `git push origin main`.
- Do not force-push `main` or merge a PR unless the user explicitly requests merging.
- Leave unrelated local files and changes out of commits.
