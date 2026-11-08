usermod -aG sudo huai
# Copy kernel and initrd with version
cp /boot/vmlinuz-6.1.0-52-amd64 /boot/efi/EFI/debian/
cp /boot/initrd.img-6.1.0-52-amd64 /boot/efi/EFI/debian/

# Create NVRAM boot entry using versioned filenames
efibootmgr -c -d /dev/nvme0n1 -p 1 -L "Debian EFISTUB" -l '\EFI\debian\vmlinuz-6.1.0-52-amd64' -u "root=UUID=$(blkid -s UUID -o value /dev/nvme0n1p2) ro quiet initrd=\EFI\debian\initrd.img-6.1.0-52-amd64"


# Force remove including essential packages
apt purge grub-common grub-efi-amd64 grub-efi-amd64-bin grub-efi-amd64-signed grub2-common os-prober shim-signed -y --allow-remove-essential

# Clean up automatically installed packages
apt autoremove --purge -y

curl -fLo ~/.vim/autoload/plug.vim --create-dirs https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim

timedatectl set-timezone UTC


# 默认注释了源码镜像以提高 apt update 速度，如有需要可自行取消注释
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware
# deb-src https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware

deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware
# deb-src https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware

deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-backports main contrib non-free non-free-firmware
# deb-src https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-backports main contrib non-free non-free-firmware

# 以下安全更新软件源为镜像站配置
deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware
# deb-src https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware
