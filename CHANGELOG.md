# Changelog

All notable changes to this project will be documented in this file.

## [1.1.4] - 2026-04-22

### Changed
- 🔧 **OpenAI Full URL Backend**: 修改 `edit` 方法的默认行为，所有 OpenAI 兼容提供商默认使用 `json_image_array` 模式传递多图，而非拼接成单张图片
- 此改动不影响豆包（火山引擎 ARK）的现有实现，豆包原本就使用 `json_image_array` 模式

## [1.1.3] - Previous Release

### Note
- Version history not documented before 1.1.4
