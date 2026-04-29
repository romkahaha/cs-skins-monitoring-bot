# External Clock

GitHub Actions `schedule` can be delayed or dropped. For critical unattended runs,
use an external clock to dispatch the workflow through the GitHub API.

## Nightly

Create a fine-grained GitHub token with access to this repository and permission:

```text
Actions: read/write
Contents: read
```

Set it as an environment variable on the server:

```bash
export GITHUB_DISPATCH_TOKEN="github_pat_..."
```

Run:

```bash
python -B automation/server/dispatch_workflow.py \
  --repo romkahaha/books \
  --workflow library-nightly.yml \
  --ref main \
  --input skip_risk=false \
  --input skip_base=true
```

Cron example for 00:15 Prague server time:

```cron
15 0 * * * cd /path/to/books && git pull --rebase && python -B automation/server/dispatch_workflow.py --repo romkahaha/books --workflow library-nightly.yml --ref main --input skip_risk=false --input skip_base=true
```
