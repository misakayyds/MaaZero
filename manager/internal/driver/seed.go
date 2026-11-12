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
name = "基建换班"
type = "Infrast"
params = { mode = 0, facility = ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm"], drones = "_NotUse", threshold = 0.3, dorm_trust_enabled = false, dorm_notstationed_enabled = true, replenish = true, reception_message_board = true, reception_clue_exchange = true, reception_send_clue = true, continue_training = true }

[[tasks]]
name = "理智作战"
type = "Fight"
params = { stage = "", medicine = 0, medicine_expire_days = 0, stone = 0, times = 999, series = 0, DrGrandet = false, server = "CN" }

[[tasks]]
name = "自动公招"
type = "Recruit"
params = { refresh = true, select = [4, 5], confirm = [3, 4, 5], times = 4, set_time = true, expedite = false, extra_tags_mode = 0, skip_robot = true, server = "CN" }

[[tasks]]
name = "信用收支"
type = "Mall"
params = { shopping = true, buy_first = ["招聘许可", "龙门币"], blacklist = ["家具零件", "加急许可"], force_shopping_if_credit_full = true, visit_friends = true, only_buy_discount = false, reserve_max_credit = false }

[[tasks]]
name = "领取奖励"
type = "Award"
params = { award = true, mail = true, recruit = false, orundum = false, mining = false, specialaccess = false }

[[tasks]]
name = "关闭游戏"
type = "CloseDown"
params = { client_type = "Official" }
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
