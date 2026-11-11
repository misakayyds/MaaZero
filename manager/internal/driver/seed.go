package driver

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const defaultCLITOML = `[core]
channel = "Stable"
test_time = 0

[cli]
channel = "Stable"

[resource]
auto_update = false
`

const defaultProfileTOML = `[connection]
adb_path = "adb"
address = "127.0.0.1:5555"
config = "CompatPOSIXShell"

[instance_options]
touch_mode = "MaaTouch"
deployment_with_pause = false
adb_lite_enabled = false
kill_adb_on_exit = false

[static_options]
cpu_ocr = true
`

const defaultDailyTaskTOML = `[[tasks]]
name = "开始唤醒"
type = "StartUp"
params = { client_type = "Official", start_game_enabled = true }

[[tasks]]
name = "领取奖励"
type = "Award"
params = { enable = true }
`

// seedDefaultConfigs creates minimal working maa-cli configuration files inside
// <dataRoot>/data/config when they do not exist yet. Existing user files are
// never overwritten.
func seedDefaultConfigs(dataRoot string) error {
	configRoot := filepath.Join(dataRoot, "data", "config")
	seeds := []struct {
		rel  string
		body string
	}{
		{rel: "cli.toml", body: defaultCLITOML},
		{rel: filepath.Join("profiles", "default.toml"), body: defaultProfileTOML},
		{rel: filepath.Join("tasks", "daily.toml"), body: defaultDailyTaskTOML},
	}

	for _, item := range seeds {
		target := filepath.Join(configRoot, item.rel)
		if _, err := os.Stat(target); err == nil {
			continue
		} else if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("stat config seed %s: %w", target, err)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return fmt.Errorf("mkdir config dir for %s: %w", target, err)
		}
		if err := os.WriteFile(target, []byte(item.body), 0600); err != nil {
			return fmt.Errorf("write config seed %s: %w", target, err)
		}
	}
	return nil
}
