# TechLead Sprint & Engineering Guidelines

## Sprint Workflow
- Daily standup syncs focus on blocking dependencies and PR reviews.
- Branch naming convention: `feature/`, `bugfix/`, or `refactor/` followed by short descriptive slug.
- PRs require at least one approving review and all unit test checks passing.

## Rollback Procedures
- If a production release encounters critical failure, trigger immediate git rollback to previous tag.
- Re-run CI deployment pipeline and verify `/health` endpoint status.
- Document post-mortem root cause analysis within 24 hours.
