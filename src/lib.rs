//! Zed 扩展壳：只做一件事——告诉 Zed「有这么个 language server，用 python 跑它」。
//!
//! 脚本内容在编译期用 `include_str!` 嵌进 WASM，运行时通过 `python3 -c` 直接执行，
//! 不依赖任何绝对路径。所以产物是自包含的：拷到任何机器都能用，
//! 那台机器不需要有这份源码、也不需要 Rust 工具链。
//!
//! Java 和 Python 各注册一个 language server，共用同一份脚本，用 `--lang` 区分。
//! 这样两个语言服务器各自只扫自己的文件，不会互相污染，也不会出现重复结果。

use zed_extension_api as zed;

/// 脚本全文嵌入 WASM（约 16KB），运行时作为 `-c` 的参数传给 python3。
const LSP_SCRIPT: &str = include_str!("../lsp/endpoint_lsp.py");

struct EndpointNav;

impl zed::Extension for EndpointNav {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        language_server_id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<zed::Command> {
        // 优先用用户 PATH 里的 python3，找不到就退回系统自带那个
        let python = worktree
            .which("python3")
            .unwrap_or_else(|| "/usr/bin/python3".to_string());

        let lang = match language_server_id.as_ref() {
            "python-endpoint-nav" => "python",
            _ => "java",
        };

        Ok(zed::Command {
            command: python,
            args: vec![
                "-c".to_string(),
                LSP_SCRIPT.to_string(),
                "--lang".to_string(),
                lang.to_string(),
            ],
            env: Default::default(),
        })
    }
}

zed::register_extension!(EndpointNav);
