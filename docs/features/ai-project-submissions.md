# AI Project Submissions with R2 Storage

This document describes the AI Project Submissions feature, including R2 bucket configuration for storing student submissions and graded artifacts.

## Overview

The AI Project Submissions feature enables students to submit projects for AI-powered grading and feedback. Submissions and their associated artifacts are stored in Cloudflare R2 object storage for durability and scalability.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Dashboard     │────▶│  Submission API  │────▶│   R2 Storage    │
│   (Frontend)    │     │  (Control Plane) │     │  (Cloudflare)   │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                │
                                ▼
                        ┌──────────────────┐
                        │   Grading Bot    │
                        │   (AI Service)   │
                        └──────────────────┘
```

## R2 Bucket Configuration

### Required Settings

| Setting | Description | Example |
|---------|-------------|---------|
| `R2_BUCKET_NAME` | Name of the R2 bucket | `nexusai-submissions` |
| `R2_ACCOUNT_ID` | Cloudflare Account ID | `abc123...` |
| `R2_ACCESS_KEY_ID` | R2 Access Key ID | `key-xyz...` |
| `R2_SECRET_ACCESS_KEY` | R2 Secret Access Key | `secret-xyz...` |
| `R2_PUBLIC_URL` | Public URL prefix (optional) | `https://pub-...r2.dev` |

### Environment Variables

Add the following to your `.env` file:

```bash
# R2 Storage Configuration
NEXUSAI_R2_BUCKET_NAME=nexusai-submissions
NEXUSAI_R2_ACCOUNT_ID=<your-cloudflare-account-id>
NEXUSAI_R2_ACCESS_KEY_ID=<your-r2-access-key>
NEXUSAI_R2_SECRET_ACCESS_KEY=<your-r2-secret-key>
NEXUSAI_R2_PUBLIC_URL=https://pub-<hash>.r2.dev
```

### Creating R2 Credentials

1. Log in to Cloudflare Dashboard
2. Navigate to **R2** → **Buckets**
3. Create a new bucket (e.g., `nexusai-submissions`)
4. Go to **R2** → **API Tokens** → **Create API Token**
5. Select **Object Read & Write** permissions
6. Scope to the specific bucket for least privilege
7. Copy the Access Key ID and Secret Access Key

### Bucket Lifecycle Policies

Configure lifecycle policies to manage storage costs:

| Policy | Description | Recommended Value |
|--------|-------------|-------------------|
| Transition to IA | Move old submissions to Infrequent Access | 30 days |
| Delete expired | Remove submissions after retention period | 365 days |

## Submission API Endpoints

### POST `/api/submissions`

Create a new submission.

**Request:**
```json
{
  "project_id": "proj-123",
  "assignment_id": "assign-456",
  "user_id": "user-789",
  "file_paths": ["src/main.py", "tests/test_main.py"],
  "metadata": {
    "branch": "main",
    "commit_hash": "abc123"
  }
}
```

**Response:**
```json
{
  "id": "sub-uuid",
  "status": "submitted",
  "submitted_at": "2025-01-15T10:30:00Z",
  "storage_url": "s3://nexusai-submissions/proj-123/sub-uuid/"
}
```

### GET `/api/submissions/{id}`

Retrieve submission details.

**Response:**
```json
{
  "id": "sub-uuid",
  "project_id": "proj-123",
  "assignment_id": "assign-456",
  "user_id": "user-789",
  "status": "graded",
  "submitted_at": "2025-01-15T10:30:00Z",
  "grade": {
    "id": "grade-uuid",
    "score": 87.5,
    "max_score": 100,
    "feedback": {...}
  },
  "artifacts": [
    {
      "type": "code",
      "file_name": "main.py",
      "storage_key": "proj-123/sub-uuid/code/main.py",
      "public_url": "https://pub-...r2.dev/proj-123/sub-uuid/code/main.py"
    }
  ]
}
```

### POST `/api/submissions/{id}/grade`

Trigger AI grading for a submission.

**Request:**
```json
{
  "rubric_id": "rubric-123",
  "model": "claude-sonnet-4-6",
  "options": {
    "include_detailed_analysis": true,
    "include_class_comparison": false
  }
}
```

### GET `/api/grading/{submission_id}`

Retrieve grading results (used by GradeView component).

**Response:**
```json
{
  "id": "grade-uuid",
  "submission_id": "sub-uuid",
  "grade": 87.5,
  "maxGrade": 100,
  "feedback": {
    "summary": "Strong implementation with minor edge cases missing.",
    "strengths": ["Clean code structure", "Good test coverage"],
    "improvements": ["Add input validation", "Handle edge cases"],
    "detailedAnalysis": "...",
    "rubricBreakdown": [...],
    "classComparison": {...},
    "aiSuggestions": [...]
  },
  "gradedAt": "2025-01-15T11:00:00Z",
  "gradedByModel": "claude-sonnet-4-6"
}
```

## Storage Structure

Files are organized in R2 using the following hierarchy:

```
nexusai-submissions/
├── {project_id}/
│   ├── {submission_id}/
│   │   ├── code/
│   │   │   └── {file_path}
│   │   ├── documents/
│   │   │   └── {file_path}
│   │   └── metadata.json
│   └── gradings/
│       └── {grading_id}/
│           ├── feedback.json
│           └── rubric_scores.json
```

## Security Considerations

### Access Control

- R2 credentials should be stored in the NexusAI vault, not in plain `.env`
- Use bucket-level IAM policies to restrict access
- Enable R2 bucket versioning for audit trails

### Signed URLs

For private submissions, generate signed URLs with expiration:

```python
from control_plane.storage.r2_client import R2Client

r2 = R2Client()
signed_url = r2.generate_presigned_url(
    bucket="nexusai-submissions",
    key="proj-123/sub-uuid/code/main.py",
    expiration_seconds=3600,
)
```

### Content Validation

All uploaded files should be validated:
- File type allowlist (`.py`, `.js`, `.md`, `.pdf`, etc.)
- Size limits (default: 10MB per file)
- Virus scanning (optional, via Lambda trigger)

## Grading Integration

The grading system integrates with the submissions API:

1. **Submission Created**: Files uploaded to R2, database record created
2. **Grading Triggered**: Grading bot fetches files from R2
3. **AI Analysis**: Model processes code/documents against rubric
4. **Results Stored**: Feedback saved to database and R2
5. **Notification**: Student notified via dashboard/email

### GradeView Component

The frontend `GradeView` component (`dashboard/static/components/GradeView.js`) displays grading results:

```javascript
const gradeView = new GradeView({
  container: document.getElementById('grade-container'),
  isPremium: user.isPremium,
  apiEndpoint: '/api/grading',
  csrfToken: getCsrfToken(),
});

await gradeView.loadGradingData(submissionId);
```

## Monitoring

### Metrics to Track

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `submissions.count` | Total submissions per hour | - |
| `submissions.size_bytes` | Storage consumption | > 10GB |
| `grading.latency_ms` | Time to grade submission | > 60s |
| `r2.errors` | R2 API failures | > 1% |

### Logging

Key log points:
- Submission creation: `submission.created`
- File upload complete: `submission.files_uploaded`
- Grading started: `grading.started`
- Grading complete: `grading.completed`
- R2 operation: `r2.{upload,download,delete}.{success,error}`

## Troubleshooting

### Common Issues

| Issue | Cause | Resolution |
|-------|-------|------------|
| `R2_ACCESS_DENIED` | Invalid credentials | Rotate R2 API token |
| `SUBMISSION_NOT_FOUND` | Wrong ID or deleted | Check database records |
| `GRADING_TIMEOUT` | Model unavailable | Retry or switch model |
| `FILE_TOO_LARGE` | Exceeds size limit | Compress or split files |

### R2 Connectivity Test

```bash
# Test R2 access with AWS CLI
aws s3 ls s3://nexusai-submissions/ \
  --endpoint-url https://<account-id>.r2.cloudflarestorage.com \
  --access-key-id <key> \
  --secret-access-key <secret>
```

## Related Documentation

- [Submissions Schema Migration](../database/submissions-schema-migration.md)
- [Database Engineer API](../database/database-engineer-api.md)
- [GradeView Component](../frontend/gradeview-component.md)
