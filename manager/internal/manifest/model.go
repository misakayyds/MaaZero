package manifest

type ChannelManifest struct {
	SchemaVersion           int                       `json:"schema_version"`
	Sequence                uint64                    `json:"sequence"`
	Channel                 string                    `json:"channel"`
	GeneratedAt             string                    `json:"generated_at"`
	MinimumBootstrapVersion string                    `json:"minimum_bootstrap_version"`
	Notices                 []Notice                  `json:"notices"`
	BlockedVersions         []BlockedVersion          `json:"blocked_versions"`
	Components              []Component               `json:"components"`
	UpstreamPolicy          map[string]UpstreamPolicy `json:"upstream_policy"`
}

type Notice struct {
	ID       string `json:"id"`
	Severity string `json:"severity"`
	Title    string `json:"title"`
	Message  string `json:"message"`
	URL      string `json:"url,omitempty"`
}

type BlockedVersion struct {
	Component string `json:"component"`
	Version   string `json:"version"`
	Reason    string `json:"reason"`
}

type Component struct {
	ID            string        `json:"id"`
	Kind          string        `json:"kind"`
	Version       string        `json:"version"`
	Required      bool          `json:"required"`
	InstallMode   string        `json:"install_mode"`
	Compatibility Compatibility `json:"compatibility"`
	Artifacts     []Artifact    `json:"artifacts"`
	HealthChecks  []string      `json:"health_checks"`
}

type Compatibility struct {
	Architectures []string        `json:"architectures"`
	AndroidAPI    AndroidAPIRange `json:"android_api"`
	RootManagers  []string        `json:"root_managers"`
}

type AndroidAPIRange struct {
	Minimum int `json:"minimum"`
	Maximum int `json:"maximum"`
}

type Artifact struct {
	URL         string     `json:"url"`
	Mirrors     []string   `json:"mirrors"`
	SHA256      string     `json:"sha256"`
	Size        int64      `json:"size"`
	MediaType   string     `json:"media_type"`
	Compression string     `json:"compression"`
	Provenance  Provenance `json:"provenance"`
}

type Provenance struct {
	Publisher  string `json:"publisher"`
	SourceURL  string `json:"source_url"`
	LicenseURL string `json:"license_url"`
}

type UpstreamPolicy struct {
	Strategy        string `json:"strategy"`
	Channel         string `json:"channel"`
	Automatic       bool   `json:"automatic"`
	AllowedVersions string `json:"allowed_versions,omitempty"`
}

type DeviceFacts struct {
	Architecture string
	AndroidAPI   int
	RootManager  string
}
