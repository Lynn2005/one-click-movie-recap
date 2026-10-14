# Cloudflare R2 setup for One Click Movie Recap

This app can keep completed MP4 files and job metadata in a private Cloudflare R2 bucket. API credentials are read only from hosting environment variables; do not commit them to GitHub or paste them into chat.

## 1. Create a private R2 bucket

1. Sign in to the Cloudflare dashboard.
2. Open **R2 Object Storage** and create a bucket (for example, `lynn-recap-videos`).
3. Keep the bucket private. The app uses short-lived signed URLs for preview and download, so a public bucket is not required.
4. In **Manage R2 API Tokens**, create a token scoped to this bucket with **Object Read & Write** permissions. Copy the Access Key ID and Secret Access Key when shown.
5. Find your Cloudflare account ID in the dashboard.

## 2. Add environment variables to Render

Open the Render web service for this repository, then **Environment → Add Environment Variable**. Add:

- `R2_ACCOUNT_ID` = your Cloudflare account ID
- `R2_ACCESS_KEY_ID` = the R2 token's Access Key ID
- `R2_SECRET_ACCESS_KEY` = the R2 token's Secret Access Key
- `R2_BUCKET_NAME` = the exact bucket name you created

Save changes and deploy the latest commit. Never add these values to source files, screenshots, or Git commits.

## 3. What the app stores

- Completed MP4: `jobs/<job-id>/movie_recap.mp4`
- Job metadata: `jobs/<job-id>/job.json`

When R2 credentials are configured, the app uploads the completed MP4 and saves job metadata in R2. The saved-video list can then be rebuilt after a Render restart/redeploy. Preview and download routes issue a one-hour signed URL for private R2 objects.

## 4. Verify it

1. Open the app and generate a short test recap.
2. Wait for the job to show **done**.
3. Confirm the saved video appears under **Saved Final Videos** and can be previewed/downloaded.
4. In Cloudflare R2, confirm the two objects exist under that job's `jobs/<job-id>/` prefix.

The R2 status indicator only means the required environment variables are present; a real test upload is still needed to verify token permissions and bucket configuration. Existing videos stored only on the old Render filesystem are not automatically copied to R2.