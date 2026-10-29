# didaktik-bootstrap

Первичная настройка Linux-сервера: zsh, Oh My Zsh и Powerlevel10k для пользователя и root.
Один статический бинарник без зависимостей, собирается из Go.

## Поддерживаемые системы

- Debian 11+
- Ubuntu 22.04+
- AlmaLinux 9+, Rocky Linux 9+, RHEL 9+
- Fedora 39+

CentOS не поддерживается.

## Установка

Скачайте бинарник из [последнего релиза](https://github.com/didaktikm/didaktik-bootstrap/releases/latest),
проверьте контрольную сумму и запустите от root:

```bash
curl -fsSLO https://github.com/didaktikm/didaktik-bootstrap/releases/latest/download/provisioning-linux-amd64
curl -fsSLO https://github.com/didaktikm/didaktik-bootstrap/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
chmod +x provisioning-linux-amd64
sudo ./provisioning-linux-amd64 didaktik
```

Аргумент — существующий пользователь (по умолчанию `didaktik`). Настройка выполняется и для него, и для root.
Повторный запуск ничего не меняет, если конфиги уже актуальны.

Для arm64 используйте `provisioning-linux-arm64`.

## Что делает

1. Ставит пакеты через `apt` (Debian/Ubuntu) или `dnf` (RHEL-семейство, Fedora). На RHEL-семействе подключает EPEL.
2. Клонирует Oh My Zsh, Powerlevel10k и плагины, каждый на закреплённом коммите.
3. Пишет `~/.zshrc` и `~/.config/zsh/provisioning-prompt.zsh`. Личные правки — в `~/.config/zsh/local.zsh`, он не перезаписывается.
4. Перед заменой файла делает бэкап: оригинал в `*.before-provisioning`, каждая замена в `*.bak.<время>`.
5. Кэширует completion для kubectl и helm.
6. Делает zsh оболочкой входа.

## Сборка

Нужен Go (версия из `go.mod`).

```bash
make test          # vet, unit tests
make dist          # dist/provisioning-linux-{amd64,arm64} и SHA256SUMS
make dist VERSION=0.1.0
./dist/provisioning-linux-amd64 --version
```

## Релиз

```bash
git tag v0.1.0
git push origin v0.1.0
```

Workflow `Release` собирает бинарники и публикует релиз с файлами и `SHA256SUMS`.
CI (`.github/workflows/ci.yml`) на каждом push и PR запускает тесты, сборку и прогон бинарника
в контейнерах Debian 12, Ubuntu 24.04, AlmaLinux 9, Rocky Linux 9 и Fedora 41, включая повторный запуск.

## Закреплённые версии

Версии зафиксированы в `repos.go`. Чтобы обновить: поменяйте значение, посмотрите diff в upstream, соберите и выпустите новый тег.
