# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed
- **Dependencies**: Upgraded `python-dotenv` to `1.2.2`. 
  - *Note on default behavior*: `python-dotenv` 1.2.2 introduces changes to symlink resolution during `.env` discovery, and stops forcing `0o600` file permissions on newly written or updated `.env` files. 
  - *Impact on HookWise*: HookWise reads the `.env` file from the default path (which does not depend on symlink resolution) and does not write to `.env` programmatically. Therefore, these upstream changes do not require codebase modifications, and `load_dotenv()` will continue to function without passing `follow_symlinks=True`.
