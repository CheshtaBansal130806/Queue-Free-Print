# GitHub Push Guide — QueueFree

- `.env` contains the project's local/private credentials and MUST NOT be committed.
- `.env.example` is the safe template for GitHub.
- `.gitignore` already ignores `.env` and generated private key files.

Before the first push:

```bash
git init
git add .
git status
```

Confirm `.env` is NOT listed. Then:

```bash
git commit -m "Initial secure QueueFree project"
git branch -M main
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

If the old repository history already contains a detected credential, use this project as a fresh Git repository rather than pushing the old history.
