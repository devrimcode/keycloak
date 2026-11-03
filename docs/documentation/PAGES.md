# Portable GitHub Pages publication

This integration adds a separate Pages workflow to the existing Keycloak documentation setup. It does not replace the upstream documentation build/check workflow.

## Repository portability

The workflow reads `GITHUB_REPOSITORY` at runtime. It derives the GitHub Pages URL and the `Edit this section` target from the current fork's owner and repository name. It does not contain a user-specific owner or repository name as a deployment identity. For a repository using a custom Pages domain, define the Actions repository variable `PAGES_BASE_URL` with the public HTTPS base URL, without a trailing slash.

`Report an issue` intentionally continues to use the official Keycloak issue tracker.

## Build all or selected guides

A push to the repository's default branch that changes a documentation/build input automatically builds all six guides and deploys the result.

To build a subset, open **Actions → Build and publish selected Keycloak documentation → Run workflow**. Enter `all` or a comma-separated list of guide IDs:

- `api_documentation`
- `authorization_services`
- `server_admin`
- `server_development`
- `release_notes`
- `upgrading`

For example, `server_admin,authorization_services` builds only those two Maven documentation modules. The other guide routes remain present as redirects to the official Keycloak documentation, so cross-guide links do not lead to missing pages.

The JavaDocs and REST API reference are built automatically for `all` or whenever `api_documentation` is selected. For another partial selection, the `api_reference` checkbox can opt into that additional build.

A manual run from the default branch deploys. A manual run from another branch builds and validates but does not publish.

## URL layout

- `/<repository>/` redirects to `/docs/nightly/` for a normal project Pages site.
- `/docs/nightly/` is the generated guide index.
- `/docs/nightly/<guide>/` contains a selected guide or redirects to the official guide if it was not selected.
- `/docs-api/nightly/` contains JavaDocs and REST API when that build is enabled.

For a user/organization site repository named `<owner>.github.io`, the base URL has no repository path prefix.

## GitHub Pages setting

After merging the integration, open **Settings → Pages → Build and deployment** and choose **GitHub Actions** as the source. The workflow publishes only from the repository's default branch.

## Limits and verification

The workflow validates generated local HTML/CSS links, fragments, source edit targets, archive safety, symlinks/hardlinks, noindex metadata and the GitHub Pages 1 GB payload limit. It then smoke-tests the deployed URLs.
The Pages artifact upload explicitly includes hidden files so that `.nojekyll` is retained; the deployment job has `actions: read`, `pages: write` and `id-token: write` permissions.

The helper tests use synthetic archive fixtures. A successful local test run is not a substitute for the first real Maven build and GitHub Pages deployment in the target fork.
