# 通道清单发布目录

本目录是稳定/测试通道清单的仓库内源位置。当前还没有真实产物版本和下载地址，因此不创建可发布的 `stable.json` 或 `beta.json`。

后续发布流程：

1. CI根据已验收组件生成 `stable.json` 或 `beta.json`。
2. 每次发布递增对应通道的 `sequence`，不得复用或倒退。
3. CI使用受保护的Ed25519私钥签名清单原始字节，生成同名 `.sig`。
4. 将四个静态文件发布到GitHub Pages：

   ```text
   channels/stable.json
   channels/stable.sig
   channels/beta.json
   channels/beta.sig
   ```

5. Root模块只内置固定GitHub Pages地址和Ed25519公钥，不内置签名私钥。

GitHub Pages只承载小型清单和签名。组件大文件继续由清单中的GitHub Releases、官方上游或对象存储地址提供。

设备需要分别记住stable和beta已接受的最高`sequence`。发现远端序号更低时拒绝更新并保留当前安装；该持久化和恢复逻辑属于安装事务Task 2。
