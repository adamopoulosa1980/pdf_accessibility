# Contributing to pdf_a11y

Thanks for considering a contribution. The project is maintained by
**ASSERT I.K.E.** and is open to outside contributors.

## Ways to contribute

- **Bug reports** — open a [GitHub issue](https://github.com/adamopoulosa1980/pdf_accessibility/issues)
  with a minimal reproducer (a small PDF that triggers the problem if you
  can share one, plus the relevant `<name>_report.json` from `output/`).
- **Pull requests** — small, focused changes are easier to review. For
  anything larger than a quick fix, open an issue first so we can agree
  on the approach before you invest time.
- **Documentation** — README clarifications, typo fixes, additional
  troubleshooting entries, and translated screenshots are all welcome.
- **Validator-profile work** — additional validator integrations
  (Section 508 ACR, EN 301 549, PDF/UA-2) are on the roadmap; if you
  want to drive one, say so on the issue tracker.

## Licensing of your contribution

By submitting a pull request you agree that your contribution is
licensed under the same **Apache License, Version 2.0** that covers the
rest of the project (see [LICENSE](LICENSE)). This is the "inbound =
outbound" convention — no separate Contributor License Agreement is
required for code contributions.

If your contribution includes substantial third-party code, please
update [NOTICE](NOTICE) accordingly so the attribution chain stays
clean.

## Development setup

1. Clone the repo and install Python deps:

   ```bash
   pip install -r requirements.txt -r webapp/requirements.txt
   ```

2. Install veraPDF locally:

   ```bash
   # Windows
   .\scripts\install-verapdf.ps1
   # Linux / macOS
   ./scripts/install-verapdf.sh
   ```

3. Run the bundled example to verify everything works:

   ```bash
   python -m pdf_a11y "examples/a short guide to the eu-NA0522433ENN.pdf"
   ```

   Output lands in `output/`. A successful run prints
   `Summary: ... 0 errors` and writes a `<name>_a11y.pdf` that passes
   both veraPDF profiles with 0 failures.

## Style

- Python: follow the existing code style (no formatter pinned;
  reasonable PEP 8). Type hints encouraged for new public functions.
- Commit messages: present-tense, imperative ("add", "fix", "remove"),
  one-line summary under ~72 chars followed by an optional body.
- Comments: explain the **why**, not the **what** — readers can see
  the code. Pointers to the relevant WCAG / PDF/UA / WTPDF rule
  number are especially valuable in fixer files.

## Commercial questions

For commercial support, integration help, on-premises deployment, or
prioritised feature work, contact **info@assert.gr**. Those discussions
do not happen on the public issue tracker — open an issue only for
genuinely open-source-side bugs, features, or docs.

## Code of Conduct

Be respectful and constructive. Discussions stay on the technical
substance; we'll close issues or PRs that turn personal.

---

Thanks for reading. Looking forward to your contribution.
