# flake.nix
{
  description = "A Nix flake for the drive-sync Python script";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }@inputs:
    flake-utils.lib.eachDefaultSystem (system:
      let
        # 使用一个统一的 Python 版本来确保所有包的兼容性
        # 这里我们选择 python3，nixpkgs 会自动选择一个稳定的版本
        pythonVersion = "python3"; 
        pkgs = import nixpkgs {
          inherit system;
        };
        
        # 从 pkgs 中获取指定版本的 Python 解释器和包集合
        python = pkgs.${pythonVersion};
        pythonPackages = pkgs.${pythonVersion + "Packages"};

      in
      {
        # 'packages.default' 是这个 flake 的主要输出包
        packages.default = pythonPackages.buildPythonApplication {
          pname = "drive-sync";
          version = "0.2.0";

          # src 指向当前目录，Nix 会自动复制所有文件
          src = ./.;

          # Nix 会使用 setup.py 进行构建
          format = "setuptools";

          # 'propagatedBuildInputs' 用于指定 Python 依赖包
          # Nix 会确保这些包在最终的环境中可用
          propagatedBuildInputs = [
            pythonPackages.google-api-python-client
            pythonPackages.google-auth-httplib2
            pythonPackages.google-auth-oauthlib
            pythonPackages.rich
          ];
          
          meta = {
            description = "A smart sync script for Google Drive with conflict resolution";
            license = pkgs.lib.licenses.mit; # 假设是 MIT 许可证
            platforms = pkgs.lib.platforms.all;
          };
        };

        # 提供一个开发环境，方便调试
        devShells.default = pkgs.mkShell {
          inputsFrom = [ self.packages.default.${system} ];
          packages = [
            python # 确保 python 命令在 shell 中可用
          ];
        };
      }
    );
}