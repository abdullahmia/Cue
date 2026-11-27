class CueZsh < Formula
  desc "Smart AI Zsh autocomplete, project history dropdown, and CLI assistant"
  homepage "https://github.com/abdullahmia/Cue"
  url "https://github.com/abdullahmia/Cue.git", tag: "v0.2.0"
  version "0.2.0"

  uses_from_macos "python"

  def install
    chmod 0755, "bin/cue"
    pkgshare.install "cue.plugin.zsh", "bin", "engine", "zsh", "bash", "powershell", "install.sh", "uninstall.sh"
    bin.install_symlink pkgshare/"bin/cue"
  end

  def caveats
    <<~EOS
      To activate the Zsh plugin, add this line to your ~/.zshrc:
        source "#{opt_pkgshare}/cue.plugin.zsh"

      Then reload your shell:
        source ~/.zshrc
    EOS
  end

  test do
    system "#{bin}/cue", "--version"
  end
end
