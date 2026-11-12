# 通道清单发布目录

本目录存放稳定通道（`stable`）与测试通道（`beta`）的已签名清单及公开校验配置镜像，并由 `.github/workflows/pages.yml` 自动校验后发布至 GitHub Pages：

```text
manifest/
├── release.json              # 模块内置公开引导配置（manifest_url + 32 字节 Base64 公钥 + channel）
└── channels/
    ├── stable.json           # 稳定通道不可变组件清单
    ├── stable.json.sig       # stable.json 原始字节的 64 字节 Ed25519 签名
    ├── beta.json             # 测试通道不可变组件清单
    └── beta.json.sig         # beta.json 原始字节的 64 字节 Ed25519 签名
```

维护规则：

1. 使用 `packaging/generate-manifests.py` 读取真实构建产物的字节大小与 SHA-256 生成清单。
2. 每次发布新清单时必须递增对应通道的 `sequence`，不得复用或倒退（客户端持久化记录已接受最高序号以防重放降级）。
3. 使用 `packaging/sign-manifests.py` 调用仓库外受保护的 Ed25519 私钥生成 `.sig` 文件并自动执行 `manifest.LoadAndVerify` 验签。
4. GitHub Pages 仅托管上述小型静态清单与签名；二进制与归档大文件由 GitHub Releases 不可变版本标签提供。
