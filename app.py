#!/usr/bin/env python3
"""
Tenable Asset EOL Portal – zero external dependencies (Python 3.8+ stdlib only).

Data strategy:
  - Assets stored in local SQLite database (eol_data.db)
  - Full sync: fetches every asset from Tenable, replaces DB for that tenant
  - Delta sync: fetches only assets whose last_seen >= last sync date, upserts
  - /api/assets always served from DB instantly (no waiting for Tenable)
  - Sync runs in a background thread; UI polls /api/jobs/<id> for live progress
"""

import os, re, json, time, ssl, threading, logging, uuid, sqlite3, hashlib, base64
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(BASE_DIR, "config.json")
DB_FILE      = os.path.join(BASE_DIR, "eol_data.db")
TEMPLATE     = os.path.join(BASE_DIR, "templates", "index.html")
SECRETS_FILE = os.path.join(BASE_DIR, ".eol_portal_secret")
PORT         = int(os.environ.get("PORT", 5555))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eol_portal")

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode    = ssl.CERT_NONE

# ── Active jobs {job_id: {...}} ──────────────────────────────────────────────
_jobs:     dict = {}
_jobs_lock       = threading.Lock()

# ── EOL cache {product_slug: [cycles]} ──────────────────────────────────────
EOL_CACHE:    dict  = {}
EOL_CACHE_TS: dict  = {}          # {product: unix timestamp of last fetch}
EOL_LOCK             = threading.Lock()
EOL_API              = "https://endoflife.date/api"
EOL_CACHE_TTL        = 24 * 3600  # re-fetch any product older than 24 h

# ── OS → endoflife.date mapping ──────────────────────────────────────────────
OS_PATTERNS = [
    # ── Windows Server (all versions share one endoflife.date slug) ───────────
    (r"windows server 2003 r2",          "windows-server",  "2003-R2"),
    (r"windows server 2003",             "windows-server",  "2003"),
    (r"windows server 2008 r2",          "windows-server",  "2008-R2"),
    (r"windows server 2008",             "windows-server",  "2008"),
    (r"windows server 2012 r2",          "windows-server",  "2012-R2"),
    (r"windows server 2012",             "windows-server",  "2012"),
    (r"windows server 2016",             "windows-server",  "2016"),
    (r"windows server 2019",             "windows-server",  "2019"),
    (r"windows server 2022",             "windows-server",  "2022"),
    (r"windows server 2025",             "windows-server",  "2025"),
    # ── Windows Desktop ───────────────────────────────────────────────────────
    (r"windows xp",                      "windows",         "XP"),
    (r"windows vista",                   "windows",         "Vista"),
    (r"windows 7",                       "windows",         "7"),
    (r"windows 8\.1",                    "windows",         "8.1"),
    (r"windows 10",                      "windows",         "10"),
    (r"windows 11",                      "windows",         "11"),
    # ── Red Hat family ───────────────────────────────────────────────────────
    (r"red hat enterprise linux[^\d]*(\d+)\.\d", "rhel",          None),
    (r"red hat enterprise linux[^\d]*(\d+)",      "rhel",          None),
    (r"rhel[^\d]*(\d+)",                          "rhel",          None),
    (r"centos stream[^\d]*(\d+)",                 "centos-stream", None),
    (r"centos[^\d]*(\d+)",                        "centos",        None),
    (r"fedora[^\d]*(\d+)",                        "fedora",        None),
    (r"alma ?linux[^\d]*(\d+)",                   "almalinux",     None),
    (r"rocky linux[^\d]*(\d+)",                   "rocky-linux",   None),
    (r"oracle linux[^\d]*(\d+)",                  "oracle-linux",  None),
    # ── Debian family ───────────────────────────────────────────────────────
    (r"ubuntu[^\d]*(\d+\.\d+)",                   "ubuntu",        None),
    (r"debian[^\d]*(\d+)",                        "debian",        None),
    # ── SUSE ────────────────────────────────────────────────────────────────
    (r"suse linux enterprise[^\d]*(\d+)",         "sles",          None),
    (r"opensuse leap[^\d]*(\d+\.\d+)",            "opensuse",      None),
    # ── Amazon ──────────────────────────────────────────────────────────────
    (r"amazon linux 2023",                        "amazon-linux",  "2023"),
    (r"amazon linux 2",                           "amazon-linux",  "2"),
    (r"amazon linux",                             "amazon-linux",  "1"),
    # ── Alpine ──────────────────────────────────────────────────────────────
    (r"alpine linux[^\d]*(\d+\.\d+)",             "alpine-linux",  None),
    # ── macOS / BSD / Solaris ───────────────────────────────────────────────
    (r"mac os x 10\.(\d+)",                       "macos",         None),
    (r"macos[^\d]*(\d+)",                         "macos",         None),
    (r"freebsd[^\d]*(\d+)",                       "freebsd",       None),
    (r"solaris[^\d]*(\d+\.\d+)",                  "oracle-solaris", None),
]

CPE_MAP = {
    # ── Windows Desktop ──────────────────────────────────────────────────────
    "microsoft:windows_10":                  ("windows",              r"(\d+)"),
    "microsoft:windows_11":                  ("windows",              "11"),
    "microsoft:windows_xp":                  ("windows",              "XP"),
    "microsoft:windows_vista":               ("windows",              "Vista"),
    "microsoft:windows_7":                   ("windows",              "7"),
    "microsoft:windows_8.1":                 ("windows",              "8.1"),
    # ── Windows Server (single slug, cycle = year) ────────────────────────
    "microsoft:windows_server_2003":         ("windows-server",       "2003"),
    "microsoft:windows_server_2008":         ("windows-server",       "2008"),
    "microsoft:windows_server_2008_r2":      ("windows-server",       "2008-R2"),
    "microsoft:windows_server_2012":         ("windows-server",       "2012"),
    "microsoft:windows_server_2012_r2":      ("windows-server",       "2012-R2"),
    "microsoft:windows_server_2016":         ("windows-server",       "2016"),
    "microsoft:windows_server_2019":         ("windows-server",       "2019"),
    "microsoft:windows_server_2022":         ("windows-server",       "2022"),
    "microsoft:windows_server_2025":         ("windows-server",       "2025"),
    # ── Red Hat / CentOS family ───────────────────────────────────────────
    "redhat:enterprise_linux":               ("rhel",                 r"(\d+)"),
    "redhat:enterprise_linux_server":        ("rhel",                 r"(\d+)"),
    "redhat:enterprise_linux_workstation":   ("rhel",                 r"(\d+)"),
    "redhat:enterprise_linux_desktop":       ("rhel",                 r"(\d+)"),
    "centos:centos":                         ("centos",               r"(\d+)"),
    "centos:centos-stream":                  ("centos-stream",        r"(\d+)"),
    "fedora-project:fedora":                 ("fedora",               r"(\d+)"),
    "almalinux:almalinux":                   ("almalinux",            r"(\d+)"),
    "rocky-linux:rocky_linux":               ("rocky-linux",          r"(\d+)"),
    "oracle:linux":                          ("oracle-linux",         r"(\d+)"),
    # ── Debian / Ubuntu family ────────────────────────────────────────────
    "canonical:ubuntu_linux":                ("ubuntu",               r"(\d+\.\d+)"),
    "debian:debian_linux":                   ("debian",               r"(\d+)"),
    # ── SUSE / openSUSE ───────────────────────────────────────────────────
    "novell:suse_linux_enterprise_server":   ("sles",                 r"(\d+)"),
    "suse:linux_enterprise_server":          ("sles",                 r"(\d+)"),
    "opensuse:opensuse_leap":                ("opensuse",             r"(\d+\.\d+)"),
    # ── Amazon / Alpine ───────────────────────────────────────────────────
    "amazon:linux":                          ("amazon-linux",         r"(\d+)"),
    "alpinelinux:alpine_linux":              ("alpine-linux",         r"(\d+\.\d+)"),
    # ── macOS / BSD / Solaris ─────────────────────────────────────────────
    "apple:mac_os_x":                        ("macos",                r"10\.(\d+)"),
    "apple:macos":                           ("macos",                r"(\d+)"),
    "freebsd:freebsd":                       ("freebsd",              r"(\d+)"),
    "sun:solaris":                           ("oracle-solaris",       r"(\d+\.\d+)"),
    "oracle:solaris":                        ("oracle-solaris",       r"(\d+\.\d+)"),
    # ── Web / app servers ─────────────────────────────────────────────────
    "apache:http_server":                    ("apache-http-server",   r"(\d+\.\d+)"),
    "apache:tomcat":                         ("tomcat",               r"(\d+\.\d+)"),
    "nginx:nginx":                           ("nginx",                r"(\d+\.\d+)"),
    "traefik:traefik":                       ("traefik",              r"(\d+\.\d+)"),
    "haproxy:haproxy":                       ("haproxy",              r"(\d+\.\d+)"),
    # ── Languages & runtimes ──────────────────────────────────────────────
    "php:php":                               ("php",                  r"(\d+\.\d+)"),
    "python:python":                         ("python",               r"(\d+\.\d+)"),
    "ruby-lang:ruby":                        ("ruby",                 r"(\d+\.\d+)"),
    "golang:go":                             ("go",                   r"(\d+\.\d+)"),
    "perl:perl":                             ("perl",                 r"(\d+\.\d+)"),
    "nodejs:node.js":                        ("nodejs",               r"(\d+(?:\.\d+)?)"),
    "rust-lang:rust":                        ("rust",                 r"(\d+\.\d+)"),
    "microsoft:powershell_core":             ("powershell",           r"(\d+\.\d+)"),
    "microsoft:powershell":                  ("powershell",           r"(\d+\.\d+)"),
    # ── JVM ───────────────────────────────────────────────────────────────
    # JDK/JRE: pre-Java-9 CPEs use 1.X.Y versioning (1.8.0_xxx = Java 8)
    # Capture major.minor so normalizer can convert "1.8" → "8", "11.0" → "11"
    "oracle:jdk":                            ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "oracle:java_se":                        ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "oracle:jre":                            ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "sun:jre":                               ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "sun:jdk":                               ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "openjdk:openjdk":                       ("eclipse-temurin",      r"(\d+)"),
    "adoptium:temurin":                      ("eclipse-temurin",      r"(\d+)"),
    "adoptopenjdk:openjdk":                  ("eclipse-temurin",      r"(\d+)"),
    "eclipse:temurin":                       ("eclipse-temurin",      r"(\d+)"),
    "redhat:openjdk":                        ("redhat-build-of-openjdk", r"(\d+)"),
    "microsoft:build_of_openjdk":            ("microsoft-build-of-openjdk", r"(\d+)"),
    # ── Databases ─────────────────────────────────────────────────────────
    "mysql:mysql":                           ("mysql",                r"(\d+\.\d+)"),
    "postgresql:postgresql":                 ("postgresql",           r"(\d+(?:\.\d+)?)"),
    "microsoft:sql_server":                  ("mssqlserver",          r"(\d+\.\d+|\d{4})"),
    "mongodb:mongodb":                       ("mongodb",              r"(\d+\.\d+)"),
    "mariadb:mariadb":                       ("mariadb",              r"(\d+\.\d+)"),
    "redis:redis":                           ("redis",                r"(\d+\.\d+)"),
    "elastic:elasticsearch":                 ("elasticsearch",        r"(\d+\.\d+)"),
    "neo4j:neo4j":                           ("neo4j",                r"(\d+\.\d+)"),
    "influxdata:influxdb":                   ("influxdb",             r"(\d+\.\d+)"),
    "couchbase:couchbase_server":            ("couchbase-server",     r"(\d+\.\d+)"),
    "sqlite:sqlite":                         ("sqlite",               r"(\d+\.\d+)"),
    # ── Messaging / streaming ─────────────────────────────────────────────
    "apache:kafka":                          ("apache-kafka",         r"(\d+\.\d+)"),
    "apache:activemq":                       ("apache-activemq",      r"(\d+\.\d+)"),
    "rabbitmq:rabbitmq":                     ("rabbitmq",             r"(\d+\.\d+)"),
    # ── Big data ──────────────────────────────────────────────────────────
    "apache:cassandra":                      ("apache-cassandra",     r"(\d+\.\d+)"),
    # ── Frameworks ────────────────────────────────────────────────────────
    "apache:log4j":                          ("log4j",                r"(\d+\.\d+)"),
    "apache:struts":                         ("apache-struts",        r"(\d+\.\d+)"),
    "jquery:jquery":                         ("jquery",               r"(\d+\.\d+)"),
    "wordpress:wordpress":                   ("wordpress",            r"(\d+\.\d+)"),
    "drupal:drupal":                         ("drupal",               r"(\d+)"),
    "joomla:joomla!":                        ("joomla",               r"(\d+\.\d+)"),
    "pivotal_software:spring_framework":     ("spring-framework",     r"(\d+\.\d+)"),
    "vmware:spring_framework":               ("spring-framework",     r"(\d+\.\d+)"),
    "pivotal_software:spring_boot":          ("spring-boot",          r"(\d+\.\d+)"),
    "vmware:spring_boot":                    ("spring-boot",          r"(\d+\.\d+)"),
    "djangoproject:django":                  ("django",               r"(\d+\.\d+)"),
    "rubyonrails:ruby_on_rails":             ("rails",                r"(\d+\.\d+)"),
    "laravel:laravel":                       ("laravel",              r"(\d+\.\d+)"),
    "symfony:symfony":                       ("symfony",              r"(\d+\.\d+)"),
    # OpenSSL cycles use 3-part versions for 1.x (1.1.1, 1.0.2) and 2-part
    # for 3.x/4.x (3.4, 4.0).  Use an optional third group so we capture
    # "1.1.1" from "1.1.1n" and "3.4" from "3.4.0" (the startswith fallback
    # in find_eol_cycle handles the residual ".0").
    "openssl:openssl":                       ("openssl",              r"(\d+\.\d+(?:\.\d+)?)"),
    "openssl_project:openssl":               ("openssl",              r"(\d+\.\d+(?:\.\d+)?)"),
    # ── Infrastructure / DevOps ───────────────────────────────────────────
    "kubernetes:kubernetes":                 ("kubernetes",           r"(\d+\.\d+)"),
    "docker:docker":                         ("docker-engine",        r"(\d+\.\d+)"),
    "hashicorp:terraform":                   ("terraform",            r"(\d+\.\d+)"),
    "hashicorp:vault":                       ("hashicorp-vault",      r"(\d+\.\d+)"),
    "hashicorp:consul":                      ("consul",               r"(\d+\.\d+)"),
    "hashicorp:nomad":                       ("nomad",                r"(\d+\.\d+)"),
    "ansible:ansible":                       ("ansible",              r"(\d+\.\d+)"),
    "redhat:ansible_engine":                 ("ansible",              r"(\d+\.\d+)"),
    "ansible:ansible-core":                  ("ansible-core",         r"(\d+\.\d+)"),
    "envoyproxy:envoy":                      ("envoy",                r"(\d+\.\d+)"),
    "istio:istio":                           ("istio",                r"(\d+\.\d+)"),
    "grafana:grafana":                       ("grafana",              r"(\d+\.\d+)"),
    "prometheus:prometheus":                 ("prometheus",           r"(\d+\.\d+)"),
    "elastic:kibana":                        ("kibana",               r"(\d+\.\d+)"),
    "elastic:logstash":                      ("logstash",             r"(\d+\.\d+)"),
    "keycloak:keycloak":                     ("keycloak",             r"(\d+\.\d+)"),
    "jenkins:jenkins":                       ("jenkins",              r"(\d+\.\d+)"),
    "gitlab:gitlab":                         ("gitlab",               r"(\d+\.\d+)"),
    "zabbix:zabbix":                         ("zabbix",               r"(\d+\.\d+)"),
    "splunk:splunk":                         ("splunk",               r"(\d+\.\d+)"),
    "atlassian:confluence_server":           ("confluence",           r"(\d+\.\d+)"),
    "atlassian:jira":                        ("jira-software",        r"(\d+\.\d+)"),
    # ── Microsoft server products ─────────────────────────────────────────
    # .NET Framework cycles include 3-part versions (4.8.1, 4.7.2, etc.)
    "microsoft:.net_framework":              ("dotnetfx",             r"(\d+\.\d+(?:\.\d+)?)"),
    # Modern .NET (5+) uses 2-part cycles (6.0, 7.0, 8.0, 9.0)
    "microsoft:dotnet":                      ("dotnet",               r"(\d+\.\d+)"),
    "microsoft:exchange_server":             ("msexchange",           r"(\d+\.\d+)"),
    "microsoft:sharepoint_server":           ("sharepoint",           r"(\d+)"),
    # Visual Studio tracks sub-minor cycles (17.0, 17.6, 17.14 etc.) — capture major.minor
    "microsoft:visual_studio":               ("visual-studio",        r"(\d+\.\d+)"),
    # ── Virtualisation ────────────────────────────────────────────────────
    # ESXi CPE versions sometimes contain full product string prefix ("esxi_6.7")
    "vmware:esxi":                           ("esxi",                 r"(?:esxi_)?(\d+\.\d+)"),
    # vCenter CPE versions sometimes contain full product string ("vmware_vcenter_server_7.0.3_build-...")
    # Version is extracted via normalizer in parse_cpe_eol
    "vmware:vcenter_server":                 ("vcenter",              r"(\d+\.\d+)"),
    # ── Browsers ──────────────────────────────────────────────────────────
    "google:chrome":                         ("chrome",               r"(\d+)"),
    "mozilla:firefox":                       ("firefox",              r"(\d+)"),
    # Internet Explorer — endoflife.date slug is "internet-explorer"
    # (Edge and Safari are not tracked on endoflife.date)
    "microsoft:internet_explorer":           ("internet-explorer",    r"(\d+)"),

    # ── Microsoft Office & productivity ───────────────────────────────────
    # endoflife.date slug is "office"; cycles are year-based (2016, 2019, 2021, 2024).
    # CPE versions are internal build strings like "16.0.19628.20132" — the
    # _normalize_office_version() normalizer maps build numbers to year cycles.
    # Regex captures the full version string for the normalizer to parse.
    "microsoft:office":                      ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:365_apps":                    ("office",               r"(\d+(?:\.\d+)*)"),
    # Individual Office apps share the same release/EOL cycle as the suite
    "microsoft:word":                        ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:excel":                       ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:outlook":                     ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:powerpoint":                  ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:access":                      ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:onenote":                     ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:publisher":                   ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:lync":                        ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:skype_for_business":          ("office",               r"(\d+(?:\.\d+)*)"),

    # ── Vendor aliases ────────────────────────────────────────────────────────
    # Alternative vendor strings used by NVD/Tenable for the same products.
    # Without these, CPE matching silently fails when the vendor string differs.

    # nginx — NVD used "igor_sysoev" before nginx Inc. became the entity
    "igor_sysoev:nginx":                     ("nginx",                r"(\d+\.\d+)"),
    "f5:nginx":                              ("nginx",                r"(\d+\.\d+)"),

    # MySQL — Oracle acquired Sun (which owned MySQL) in 2010
    "oracle:mysql":                          ("mysql",                r"(\d+\.\d+)"),

    # PostgreSQL — full org name variant
    "postgresql_global_development_group:postgresql": ("postgresql",  r"(\d+(?:\.\d+)?)"),
    "the_postgresql_global_development_group:postgresql": ("postgresql", r"(\d+(?:\.\d+)?)"),

    # Node.js — CPE vendor/product combos vary
    "nodejs:nodejs":                         ("nodejs",               r"(\d+(?:\.\d+)?)"),
    "node.js_foundation:node.js":            ("nodejs",               r"(\d+(?:\.\d+)?)"),

    # Python — Python Software Foundation is the official NVD vendor in some CPEs
    "python_software_foundation:python":     ("python",               r"(\d+\.\d+)"),

    # Ruby — early NVD entries credited the creator as vendor
    "yukihiro_matsumoto:ruby":               ("ruby",                 r"(\d+\.\d+)"),

    # PHP
    "the_php_group:php":                     ("php",                  r"(\d+\.\d+)"),

    # Apache HTTP Server — older NVD entries use full org name
    "apache_software_foundation:http_server": ("apache-http-server",  r"(\d+\.\d+)"),

    # Docker — Moby is the open-source project
    "moby:moby":                             ("docker-engine",        r"(\d+\.\d+)"),
    "docker:docker_desktop":                 ("docker-engine",        r"(\d+\.\d+)"),

    # Kubernetes — some CPEs use linuxfoundation/cncf as vendor
    "linuxfoundation:kubernetes":            ("kubernetes",           r"(\d+\.\d+)"),
    "cncf:kubernetes":                       ("kubernetes",           r"(\d+\.\d+)"),

    # Jenkins
    "cloudbees:jenkins":                     ("jenkins",              r"(\d+\.\d+)"),
    "jenkins_ci:jenkins":                    ("jenkins",              r"(\d+\.\d+)"),

    # GitLab
    "gitlab_b.v.:gitlab":                   ("gitlab",               r"(\d+\.\d+)"),
    "gitlab_inc.:gitlab":                   ("gitlab",               r"(\d+\.\d+)"),

    # Elastic stack — some NVD entries use "elasticsearch" as vendor for all products
    "elasticsearch:elasticsearch":           ("elasticsearch",        r"(\d+\.\d+)"),
    "elasticsearch:kibana":                  ("kibana",               r"(\d+\.\d+)"),

    # Redis — Pivotal/VMware shipped Redis
    "pivotal_software:redis":               ("redis",                r"(\d+\.\d+)"),

    # WordPress
    "automattic:wordpress":                  ("wordpress",            r"(\d+\.\d+)"),

    # Spring — some CPEs use "springsource" (old company name)
    "springsource:spring_framework":        ("spring-framework",     r"(\d+\.\d+)"),
    "spring:spring_framework":              ("spring-framework",     r"(\d+\.\d+)"),
    "springsource:spring_boot":             ("spring-boot",          r"(\d+\.\d+)"),

    # Traefik — Containous was the original company
    "containous:traefik":                   ("traefik",              r"(\d+\.\d+)"),

    # RabbitMQ — Pivotal/VMware shipped RabbitMQ
    "pivotal_software:rabbitmq":            ("rabbitmq",             r"(\d+\.\d+)"),
    "vmware:rabbitmq":                      ("rabbitmq",             r"(\d+\.\d+)"),

    # Java — some NVD CPEs use "java" as vendor (not "oracle" or "sun")
    "java:jre":                             ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "java:jdk":                             ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),
    "ibm:java":                             ("oracle-jdk",           r"(\d+(?:\.\d+)?)"),

    # .NET Core / modern .NET — older NVD CPEs used the name "net_core"
    "microsoft:.net_core":                  ("dotnet",               r"(\d+\.\d+)"),

    # Internet Explorer — alternate CPE product name "ie" (as well as "internet_explorer")
    "microsoft:ie":                         ("internet-explorer",    r"(\d+)"),

    # Microsoft Office compatibility pack / converters — same lifecycle as Office
    "microsoft:office_compatibility_pack":  ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:excelcnv":                   ("office",               r"(\d+(?:\.\d+)*)"),
    "microsoft:wordcnv":                    ("office",               r"(\d+(?:\.\d+)*)"),
}

# ── CPE product-only fallback index ──────────────────────────────────────────
# Built lazily on first use. Maps CPE *product* name → (eol_slug, ver_regex)
# for entries where the product name is unambiguous (appears exactly once in
# CPE_MAP). Used as a last resort when the vendor string is unknown but the
# product name alone is enough to identify the software.
_CPE_PRODUCT_INDEX: dict | None = None
_CPE_INDEX_LOCK = threading.Lock()


def _get_cpe_product_index() -> dict:
    global _CPE_PRODUCT_INDEX
    if _CPE_PRODUCT_INDEX is not None:
        return _CPE_PRODUCT_INDEX
    with _CPE_INDEX_LOCK:
        if _CPE_PRODUCT_INDEX is not None:
            return _CPE_PRODUCT_INDEX
        from collections import Counter
        counts: Counter = Counter(k.split(":", 1)[1] for k in CPE_MAP)
        idx = {}
        for key, val in CPE_MAP.items():
            prod = key.split(":", 1)[1]
            if counts[prod] == 1:
                idx[prod] = val
        _CPE_PRODUCT_INDEX = idx
        return idx


# ── Credential encryption ─────────────────────────────────────────────────────
# Credentials are encrypted at rest using PBKDF2-derived OTP (XOR).
# A random 32-byte key is generated on first run and stored in SECRETS_FILE
# (chmod 600).  config.json stores only base64-encoded salt+ciphertext.

def _get_or_create_secret() -> bytes:
    """Return the per-install 32-byte secret key, creating it on first run."""
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
    import secrets as _sec
    key = _sec.token_bytes(32)
    with open(SECRETS_FILE, "wb") as f:
        f.write(key)
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except Exception:
        pass
    log.info("Created new encryption key at %s", SECRETS_FILE)
    return key


def _is_encrypted(s: str) -> bool:
    """Heuristic: is s an encrypted credential (base64, ≥17 decoded bytes)?"""
    if not s or len(s) < 24:
        return False
    try:
        raw = base64.b64decode(s, validate=True)
        return len(raw) >= 17   # 16-byte salt + ≥1 byte ciphertext
    except Exception:
        return False


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a credential.  Returns base64(salt‖ciphertext)."""
    if not plaintext:
        return ""
    key  = _get_or_create_secret()
    salt = os.urandom(16)
    data = plaintext.encode()
    ks   = hashlib.pbkdf2_hmac("sha256", key, salt, 100_000, dklen=len(data))
    ct   = bytes(a ^ b for a, b in zip(data, ks))
    return base64.b64encode(salt + ct).decode()


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a credential produced by encrypt_credential."""
    if not ciphertext:
        return ""
    try:
        raw      = base64.b64decode(ciphertext)
        salt, ct = raw[:16], raw[16:]
        key      = _get_or_create_secret()
        ks       = hashlib.pbkdf2_hmac("sha256", key, salt, 100_000, dklen=len(ct))
        return bytes(a ^ b for a, b in zip(ct, ks)).decode()
    except Exception:
        return ciphertext   # fallback: treat as plaintext (migration path)


# ── Database ─────────────────────────────────────────────────────────────────

def get_conn():
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with get_conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS assets (
            id             TEXT NOT NULL,
            tenant_id      TEXT NOT NULL,
            name           TEXT,
            ips            TEXT DEFAULT '[]',
            hostnames      TEXT DEFAULT '[]',
            os             TEXT,
            last_seen      TEXT,
            overall_status TEXT DEFAULT 'unknown',
            eol_entries    TEXT DEFAULT '[]',
            synced_at      REAL,
            software       TEXT DEFAULT '[]',
            attributes     TEXT DEFAULT '{}',
            PRIMARY KEY (id, tenant_id)
        );
        CREATE INDEX IF NOT EXISTS idx_assets_tenant ON assets(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(overall_status);

        CREATE TABLE IF NOT EXISTS sync_state (
            tenant_id        TEXT PRIMARY KEY,
            last_full_sync   REAL,
            last_delta_sync  REAL,
            asset_count      INTEGER DEFAULT 0,
            last_sync_mode   TEXT,
            new_in_delta     INTEGER DEFAULT 0,
            updated_in_delta INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS eol_cycles (
            product     TEXT PRIMARY KEY,
            cycles_json TEXT,
            fetched_at  REAL
        );
        """)
        # ── Schema migration: add columns that may not exist in older DBs ────
        for col, definition in [
            ("software",   "TEXT DEFAULT '[]'"),
            ("attributes", "TEXT DEFAULT '{}'"),
            ("tags",       "TEXT DEFAULT '[]'"),
        ]:
            try:
                con.execute(f"ALTER TABLE assets ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
    log.info(f"Database ready: {DB_FILE}")


# Overall risk priority: eol is worst, then eol_soon, then supported (known-good beats unknown).
# An asset with one supported entry + unknown apps → "supported", not "unknown".
# "unknown" only wins when nothing at all could be assessed.
_RISK_PRI = {"eol": 0, "eol_soon": 1, "supported": 2, "unknown": 3}

def _recompute_overall_status(eol_entries: list) -> str:
    """Derive overall_status from eol_entries so it's always consistent with detail data."""
    if not eol_entries:
        return "unknown"
    worst = min(eol_entries, key=lambda x: _RISK_PRI.get(x.get("status"), 2))
    return worst.get("status", "unknown")


def db_get_assets(tenant_id: str) -> list:
    with get_conn() as con:
        rows = con.execute(
            "SELECT * FROM assets WHERE tenant_id=? ORDER BY name",
            (tenant_id,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["ips"]         = json.loads(r["ips"]         or "[]")
        d["hostnames"]   = json.loads(r["hostnames"]   or "[]")
        d["eol_entries"] = json.loads(r["eol_entries"] or "[]")
        d["software"]    = json.loads(r["software"]    or "[]") if r["software"] else []
        d["attributes"]  = json.loads(r["attributes"]  or "{}") if r["attributes"] else {}
        d["tags"]        = json.loads(r["tags"]        or "[]") if r["tags"] else []
        # Always recompute so dashboard & filters reflect current eol_entries, not stale DB snapshot
        d["overall_status"] = _recompute_overall_status(d["eol_entries"])
        result.append(d)
    return result


def db_upsert_assets(tenant_id: str, assets: list, replace_all: bool = False):
    now = time.time()
    with get_conn() as con:
        if replace_all:
            con.execute("DELETE FROM assets WHERE tenant_id=?", (tenant_id,))
        for a in assets:
            con.execute("""
                INSERT INTO assets (id, tenant_id, name, ips, hostnames, os,
                    last_seen, overall_status, eol_entries, synced_at,
                    software, attributes, tags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id, tenant_id) DO UPDATE SET
                    name=excluded.name, ips=excluded.ips,
                    hostnames=excluded.hostnames, os=excluded.os,
                    last_seen=excluded.last_seen,
                    overall_status=excluded.overall_status,
                    eol_entries=excluded.eol_entries,
                    synced_at=excluded.synced_at,
                    software=excluded.software,
                    attributes=excluded.attributes,
                    tags=excluded.tags
            """, (
                a["id"], tenant_id, a["name"],
                json.dumps(a["ips"]), json.dumps(a["hostnames"]),
                a["os"], a["last_seen"], a["overall_status"],
                json.dumps(a["eol_entries"]), now,
                json.dumps(a.get("software", [])),
                json.dumps(a.get("attributes", {})),
                json.dumps(a.get("tags", [])),
            ))
        count = con.execute(
            "SELECT COUNT(*) FROM assets WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0]
    return count


def db_get_sync_state(tenant_id: str) -> dict:
    with get_conn() as con:
        row = con.execute(
            "SELECT * FROM sync_state WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
    return dict(row) if row else {}


def db_update_sync_state(tenant_id: str, mode: str, count: int,
                          new: int = 0, updated: int = 0):
    now = time.time()
    with get_conn() as con:
        con.execute("""
            INSERT INTO sync_state (tenant_id, last_full_sync, last_delta_sync,
                asset_count, last_sync_mode, new_in_delta, updated_in_delta)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                last_full_sync  = CASE WHEN ?='full'  THEN ? ELSE last_full_sync  END,
                last_delta_sync = CASE WHEN ?='delta' THEN ? ELSE last_delta_sync END,
                asset_count     = ?,
                last_sync_mode  = ?,
                new_in_delta    = ?,
                updated_in_delta= ?
        """, (
            tenant_id,
            now if mode == "full" else None,
            now if mode == "delta" else None,
            count, mode, new, updated,
            # UPDATE clause params
            mode, now, mode, now, count, mode, new, updated
        ))

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config; return credentials as plaintext (decrypted). Migrates plaintext creds on disk."""
    if not os.path.exists(CONFIG_FILE):
        return {"tenants": []}
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    migrate = False
    for t in cfg.get("tenants", []):
        for field in ("access_key", "secret_key"):
            val = t.get(field, "")
            if not val:
                continue
            if _is_encrypted(val):
                t[field] = decrypt_credential(val)   # decrypt for in-memory use
            else:
                migrate = True   # plaintext found — will encrypt on disk below
    if migrate:
        save_config(cfg)   # re-save with credentials encrypted
    return cfg


def save_config(cfg: dict):
    """Save config with credentials encrypted at rest."""
    import copy
    cfg_disk = copy.deepcopy(cfg)
    for t in cfg_disk.get("tenants", []):
        for field in ("access_key", "secret_key"):
            val = t.get(field, "")
            if val:
                t[field] = encrypt_credential(val)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg_disk, f, indent=2)


def tenable_headers(t: dict) -> dict:
    return {
        "X-ApiKeys": f"accessKey={t['access_key']}; secretKey={t['secret_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

# ── EOL lookups ──────────────────────────────────────────────────────────────

def http_get(url: str, headers: dict = None, timeout: int = 30):
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=timeout, context=ssl_ctx) as r:
        return json.loads(r.read().decode())


def http_post(url: str, body: dict, headers: dict = None, timeout: int = 30):
    from urllib.error import HTTPError
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req  = Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urlopen(req, timeout=timeout, context=ssl_ctx) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error(f"HTTP {e.code} from POST {url}: {body_text[:400]}")
        raise


def _fetch_eol_product(product: str, force: bool = False):
    """Fetch one product's cycle list from endoflife.date, respecting in-memory cache.

    EOL_CACHE_TS is only updated on a SUCCESSFUL fetch so that failed fetches
    are retried on the next prewarm/sync cycle rather than being treated as
    fresh (which would suppress retries for 24 h).
    """
    if not force:
        with EOL_LOCK:
            if product in EOL_CACHE:
                return
    try:
        data   = http_get(f"{EOL_API}/{product}.json", timeout=10)
        cycles = data if isinstance(data, list) else []
        # Only mark as fresh on success — failed fetches stay stale so they
        # are retried on the next sync / prewarm call.
        ts = time.time()
        with EOL_LOCK:
            EOL_CACHE[product]    = cycles
            EOL_CACHE_TS[product] = ts
        _persist_eol_product(product, cycles, ts)
    except Exception as e:
        stale = EOL_CACHE.get(product, [])
        log.warning(f"EOL fetch failed for {product!r}: {e} — "
                    f"keeping {'stale' if stale else 'empty'} cache "
                    f"({len(stale)} cycles); will retry next sync")
        with EOL_LOCK:
            # Preserve any previously-successful data; do NOT update TS so
            # the next prewarm treats this product as needing a refresh.
            if product not in EOL_CACHE:
                EOL_CACHE[product] = []  # ensure key exists to avoid KeyError


def prewarm_eol(products: set):
    """Fetch missing or stale EOL entries (respects EOL_CACHE_TTL)."""
    now    = time.time()
    needed = {p for p in products
              if p not in EOL_CACHE
              or now - EOL_CACHE_TS.get(p, 0) > EOL_CACHE_TTL}
    if not needed:
        return
    log.info(f"Pre-warming EOL for {len(needed)} product(s)…")
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_eol_product, p, True): p for p in needed}
        for f in as_completed(futures):
            try: f.result()
            except Exception: pass


def _all_eol_products() -> set:
    """Union of every endoflife.date slug referenced in OS_PATTERNS and CPE_MAP."""
    slugs = {slug for _, slug, _ in OS_PATTERNS}
    slugs.update(slug for slug, _ in CPE_MAP.values())
    return slugs


def refresh_eol_cache(force: bool = False):
    """Re-fetch stale (or all, if force=True) EOL data for every mapped product.

    Called at the start of each sync so the portal always has fresh EOL dates
    without needing a server restart.  Stale = older than EOL_CACHE_TTL (24 h).
    """
    now      = time.time()
    products = _all_eol_products()
    if force:
        stale = products
    else:
        stale = {p for p in products
                 if now - EOL_CACHE_TS.get(p, 0) > EOL_CACHE_TTL}
    if not stale:
        log.info("EOL cache is fresh — skipping refresh")
        return
    log.info(f"Refreshing EOL data: {len(stale)}/{len(products)} products "
             f"({'forced' if force else f'stale >{EOL_CACHE_TTL//3600}h'})…")
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_eol_product, p, True): p for p in stale}
        ok = err = 0
        for f in as_completed(futures):
            try:
                f.result(); ok += 1
            except Exception as e:
                log.warning(f"EOL refresh failed for {futures[f]}: {e}"); err += 1
    log.info(f"EOL cache refresh done — {ok} ok, {err} errors")


def _persist_eol_product(product: str, cycles: list, ts: float):
    """Upsert one product's EOL cycle list into the persistent DB."""
    try:
        with get_conn() as con:
            con.execute("""
                INSERT INTO eol_cycles (product, cycles_json, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(product) DO UPDATE SET
                    cycles_json = excluded.cycles_json,
                    fetched_at  = excluded.fetched_at
            """, (product, json.dumps(cycles), ts))
    except Exception as e:
        log.warning(f"Failed to persist EOL for {product!r}: {e}")


def _load_eol_from_db():
    """Seed in-memory EOL cache from the persisted eol_cycles table on startup."""
    try:
        with get_conn() as con:
            rows = con.execute(
                "SELECT product, cycles_json, fetched_at FROM eol_cycles"
            ).fetchall()
        count = 0
        with EOL_LOCK:
            for row in rows:
                EOL_CACHE[row["product"]]    = json.loads(row["cycles_json"] or "[]")
                EOL_CACHE_TS[row["product"]] = row["fetched_at"] or 0
                count += 1
        if count:
            log.info(f"Loaded EOL cache from DB: {count} products")
    except Exception as e:
        log.warning(f"Could not load EOL cache from DB: {e}")


def find_eol_cycle(product: str, version: str) -> dict | None:
    """Match a version string to the best-fitting cycle in the EOL cache.

    Three passes (in decreasing specificity):
    1. Exact string match              — "1.1.1" == "1.1.1"
    2. version starts with cycle       — "1.24.0".startswith("1.24")  → "1.24"
    3. Normalised cycle match          — strip non-numeric qualifiers
       from the *cycle* name (e.g. "3.5-sp1" → "3.5") and retry exact
       and prefix comparisons.  This covers endoflife.date quirks like
       dotnetfx "3.5-sp1" being the only entry for .NET 3.5.
    """
    if not product or not version:
        return None
    v = str(version).strip().lower()
    with EOL_LOCK:
        cycles = list(EOL_CACHE.get(product, []))

    # Pass 1 – exact match
    for c in cycles:
        if str(c.get("cycle", "")).lower() == v:
            return c

    # Pass 2 – version is a prefix of the cycle key (e.g. "3.4.0" → "3.4")
    for c in cycles:
        if v.startswith(str(c.get("cycle", "")).lower()):
            return c

    # Pass 3 – strip qualifier suffixes from cycle (e.g. "3.5-sp1" → "3.5")
    # and retry.  Only strip if the raw cycle contained a "-" or "_" separator.
    for c in cycles:
        raw_cycle = str(c.get("cycle", "")).lower()
        if "-" not in raw_cycle and "_" not in raw_cycle:
            continue  # no qualifier to strip; already tried in passes 1/2
        norm = re.split(r"[-_]", raw_cycle)[0]
        if v == norm or v.startswith(norm + ".") or v.startswith(norm + "-"):
            return c

    # Pass 4 – cycle is more specific than version (reverse prefix).
    # E.g. version="12" matches cycle "12.5" (SLES service-pack cycles).
    # Takes the first match, which is typically the most recent cycle from the API.
    for c in cycles:
        cyc = str(c.get("cycle", "")).lower()
        if cyc.startswith(v + "."):
            return c

    return None


def compute_eol_status(eol_val) -> dict:
    if eol_val is True or eol_val == "true":
        return {"status": "eol",       "eol_date": None,     "days_remaining": -1}
    if eol_val is False or eol_val == "false":
        return {"status": "supported", "eol_date": "No EOL", "days_remaining": 99999}
    try:
        eol  = date.fromisoformat(str(eol_val))
        days = (eol - date.today()).days
        return {"status": "eol" if days < 0 else ("eol_soon" if days <= 90 else "supported"),
                "eol_date": str(eol), "days_remaining": days}
    except Exception:
        return {"status": "unknown", "eol_date": str(eol_val), "days_remaining": None}

# ── OS / CPE parsing ──────────────────────────────────────────────────────────

def _os_product_version(os_str: str) -> tuple:
    s = os_str.lower()
    for pattern, product, static_ver in OS_PATTERNS:
        m = re.search(pattern, s)
        if m:
            return product, static_ver if static_ver else (m.group(1) if m.lastindex else None)
    return None, None


EOL_DATE_BASE = "https://endoflife.date"


def parse_os_eol(os_string: str) -> dict:
    product, version = _os_product_version(os_string)
    if product:
        eol_url = f"{EOL_DATE_BASE}/{product}"
        cycle = find_eol_cycle(product, version)
        if cycle:
            info = compute_eol_status(cycle.get("eol"))
            info.update({"product": product, "version": version,
                          "cycle": cycle.get("cycle"), "lts": cycle.get("lts", False),
                          "source": "os", "eol_url": eol_url})
            return info
        return {"status": "unknown", "product": product, "version": version,
                "eol_date": None, "days_remaining": None, "source": "os",
                "eol_url": eol_url}
    # No OS pattern matched — don't add a spurious "unknown" entry.
    return None


# ── Product-specific version normalizers ─────────────────────────────────────
# SQL Server: Tenable CPE versions are build major.minor ("16.0", "13.0.xxxx")
# or occasionally the product year ("2012"). endoflife.date cycles match the
# build major.minor format ("16.0", "13.0-sp3").
_MSSQL_YEAR_TO_BUILD: dict[str, str] = {
    "2025": "17.0", "2022": "16.0", "2019": "15.0", "2017": "14.0",
    "2016": "13.0", "2014": "12.0", "2012": "11.0", "2008": "10.0",
    "2005": "9.0",  "2000": "8.0",
}

# Exchange Server: Tenable CPE versions use internal build major.minor
# ("15.1.xxxx" = Exchange 2016). endoflife.date cycles use product years.
_EXCHANGE_BUILD_TO_CYCLE: dict[str, str] = {
    "15.2": "2019", "15.1": "2016", "15.0": "2013",
    "14.3": "2010", "14.2": "2010", "14.1": "2010", "14.0": "2010",
    "8.3":  "2007", "8.2":  "2007", "8.1":  "2007", "8.0":  "2007",
    "6.5":  "2003",
}

# Oracle JDK/JRE: pre-Java-9 CPE versions use 1.X.Y scheme where X is the
# real major (1.8.0_xxx = Java 8). Java 9+ uses X.Y.Z directly.
# This function normalises either form to the single major number used as
# endoflife.date cycle name (8, 11, 17, 21 …).
def _normalize_java_version(ver: str) -> str:
    # "1.X…" where X ≥ 5 → major Java version is X (not 1)
    # X < 5 stays as "1.X" because endoflife.date has cycles "1.4", "1.3" etc.
    m = re.match(r"^1\.(\d+)", ver)
    if m:
        minor = int(m.group(1))
        return str(minor) if minor >= 5 else f"1.{minor}"
    # Java 9+ or already a bare major — take first digit group
    m2 = re.match(r"^(\d+)", ver)
    return m2.group(1) if m2 else ver


# Microsoft Office: CPE versions use internal build strings like "16.0.19628.20132".
# Map (major_version, build_number) → endoflife.date year cycle.
# Microsoft 365 Apps (build >= 19000) are rolling-release with no fixed EOL — suppressed.
def _normalize_office_version(ver: str) -> str | None:
    """Map Office internal build string (e.g. 16.0.19628.20132) to year cycle."""
    # If already a bare year (some older CPEs embed it directly), pass through
    if re.match(r"^\d{4}$", ver):
        return ver
    # Parse "MAJOR.0.BUILD" or "MAJOR.0.BUILD.UPDATE" format
    m = re.match(r"^(\d+)\.0\.(\d+)", ver)
    if m:
        major, build = int(m.group(1)), int(m.group(2))
        if major == 16:
            if build >= 19000:   return None    # Microsoft 365 Apps — rolling, suppress
            elif build >= 17000: return "2024"
            elif build >= 14000: return "2021"
            elif build >= 10000: return "2019"
            else:                return "2016"
        elif major == 15: return "2013"
        elif major == 14: return "2010"
        elif major == 12: return "2007"
        elif major == 11: return "2003"
        return None
    # "MAJOR.MINOR" without build number — try major-only heuristic
    m2 = re.match(r"^(\d+)", ver)
    if m2:
        major = int(m2.group(1))
        return {16: "2016", 15: "2013", 14: "2010", 12: "2007", 11: "2003"}.get(major)
    return None


# Versions that are definitively EOL but not tracked on endoflife.date
# (typically because they predate the product's history on that site).
# Values are ISO-8601 EOL dates sourced from Microsoft/vendor documentation.
_HARDCODED_EOL: dict[tuple, str] = {
    ("dotnetfx", "1.0"): "2007-07-09",
    ("dotnetfx", "1.1"): "2008-10-14",
    ("dotnetfx", "2.0"): "2011-07-12",
}


def parse_cpe_eol(cpe: str) -> dict | None:
    if cpe.startswith("cpe:2.3:"):
        parts = cpe[8:].split(":")
        if len(parts) < 4: return None
        vendor, product, ver = parts[1], parts[2], parts[3]
    elif cpe.startswith("cpe:/"):
        rest = cpe[5:].split(":")
        if len(rest) < 3: return None
        vendor, product = rest[1], rest[2]
        ver = rest[3].split("/")[0] if len(rest) > 3 else "*"
    else:
        return None

    key = f"{vendor}:{product}"
    if key in CPE_MAP:
        eol_product, ver_regex = CPE_MAP[key]
    else:
        # Fallback: try product name alone (only when unambiguous)
        fallback = _get_cpe_product_index().get(product)
        if fallback:
            eol_product, ver_regex = fallback
            log.debug(f"CPE vendor fallback: {key!r} → matched product {product!r} → {eol_product}")
        else:
            return None

    eol_url = f"{EOL_DATE_BASE}/{eol_product}"
    version = ver
    if ver_regex and ver not in ("*", "-", ""):
        vm = re.match(ver_regex, ver)
        if vm:
            version = vm.group(1)
        else:
            log.debug(f"CPE version regex {ver_regex!r} did not match {ver!r} for {eol_product}")

    # Product-specific version normalization applied after regex extraction
    if eol_product == "mssqlserver":
        # Year-format ("2012") → build major.minor ("11.0")
        version = _MSSQL_YEAR_TO_BUILD.get(version, version)
    elif eol_product == "msexchange":
        # Build major.minor ("15.1") → product year cycle ("2016")
        mm = re.match(r"^(\d+\.\d+)", version)
        if mm:
            mapped = _EXCHANGE_BUILD_TO_CYCLE.get(mm.group(1))
            if mapped:
                version = mapped
    elif eol_product == "oracle-jdk":
        # Pre-Java-9 "1.X" → bare major "X" (e.g. "1.8" → "8")
        version = _normalize_java_version(version)
    elif eol_product == "docker-engine":
        # Docker Desktop uses 4.x versioning; docker-engine does not have 4.x cycles.
        if re.match(r"^[4-9]\.", version):
            return None
        # Pre-CalVer Docker Engine (1.x) predates endoflife.date tracking; suppress.
        if re.match(r"^1\.", version):
            return None
        # Old Docker CalVer used unpadded months: "17.6" → "17.06"
        dm = re.match(r"^(\d{2})\.(\d)$", version)
        if dm:
            version = f"{dm.group(1)}.0{dm.group(2)}"
    elif eol_product == "dotnetfx":
        # Strip Windows build number suffix: "2.0.50727" → "2.0", "3.0.6920" → "3.0"
        m_dnfx = re.match(r"^(\d+\.\d+)", version)
        if m_dnfx:
            version = m_dnfx.group(1)
        # Bare integer (e.g. "4") → try "4.0" (.NET Framework 4.x family)
        elif re.match(r"^\d+$", version):
            version = version + ".0"
        # Suppress bogus high-version DLL misattributions — .NET Framework max is 4.8.x
        try:
            if float(version.split(".")[0]) >= 5:
                return None
        except Exception:
            pass
        # .NET 3.0 was superseded by .NET 3.5 SP1 (same support lifecycle).
        # endoflife.date only tracks "3.5-sp1"; map 3.0 so it resolves correctly.
        if version == "3.0":
            version = "3.5"
    elif eol_product == "openssl":
        # Tenable sometimes emits garbage OpenSSL versions ("8l", "2q", "954nch", etc.)
        # that are Windows DLL version fields, not real OpenSSL versions.
        if not re.match(r"^\d+\.\d+", version):
            return None
        # OpenSSL major versions above 3 don't exist; suppress (DLL misattribution).
        try:
            if int(version.split(".")[0]) > 3:
                return None
        except Exception:
            pass
    elif eol_product == "sqlite":
        # Real SQLite versions are always 3.x.x; anything else is DLL misattribution.
        try:
            if int(version.split(".")[0]) != 3:
                return None
        except Exception:
            pass
    elif eol_product == "office":
        # Internal build string → year cycle; returns None for M365 Apps (rolling release)
        version = _normalize_office_version(version)
        if version is None:
            return None
    elif eol_product == "nginx":
        # Nginx is always 1.x.x; other version numbers are Windows DLL misattributions.
        if not version.startswith("1."):
            return None
    elif eol_product == "mysql":
        # MySQL 6.x was never released as a GA product; suppress it.
        if version.startswith("6."):
            return None
    elif eol_product == "ruby":
        # Ruby max release is 3.x; higher versions are bogus DLL misattributions.
        try:
            if int(version.split(".")[0]) > 3:
                return None
        except Exception:
            pass
    elif eol_product == "vcenter":
        # Some CPEs embed full product string: "vmware_vcenter_server_7.0.3_build-..."
        # Extract the version number wherever it appears in the string
        if not re.match(r"^\d", version):
            vm2 = re.search(r"(\d+\.\d+(?:\.\d+)?)", version)
            if vm2:
                version = vm2.group(1)

    # If the EOL cache was successfully fetched but is empty, the product is not
    # tracked on endoflife.date — return None so the entry is suppressed.
    with EOL_LOCK:
        cache_fetched = eol_product in EOL_CACHE_TS
        cache_empty   = not EOL_CACHE.get(eol_product)
    if cache_fetched and cache_empty:
        log.debug(f"CPE product {eol_product!r} not on endoflife.date — suppressing entry")
        return None

    cycle = find_eol_cycle(eol_product, version)
    if cycle:
        info = compute_eol_status(cycle.get("eol"))
        info.update({"product": eol_product, "version": version,
                     "cycle": cycle.get("cycle"), "cpe": cpe,
                     "source": "cpe", "lts": cycle.get("lts", False),
                     "eol_url": eol_url})
        return info
    if version not in ("*", "-", ""):
        log.debug(f"CPE no cycle match: {eol_product} version {version!r} (cpe={cpe!r})")

    # Fall back to hardcoded EOL dates for versions not tracked on endoflife.date.
    hardcoded_date = _HARDCODED_EOL.get((eol_product, version))
    if hardcoded_date:
        info = compute_eol_status(hardcoded_date)
        info.update({"product": eol_product, "version": version,
                     "cycle": version, "cpe": cpe,
                     "source": "cpe", "lts": False,
                     "eol_url": eol_url})
        return info

    return {"status": "unknown", "product": eol_product, "version": version,
            "eol_date": None, "days_remaining": None, "cpe": cpe,
            "source": "cpe", "eol_url": eol_url}

# ── Tenable Assets Export API ────────────────────────────────────────────────
# Uses POST /assets/export (no 5 000-asset cap, async, chunked, includes
# installed_software CPE strings so no per-asset plugin queries needed).

EXPORT_POLL_INTERVAL = 3   # seconds between status polls
EXPORT_POLL_MAX      = 120  # give up after this many polls (~6 min)


def _start_export(tenant: dict, since_ts: float | None = None) -> str:
    """POST /assets/export and return the export UUID.

    Always scoped to:
      - VM sources only  (NESSUS_SCAN = credentialed/uncredentialed scans,
                          NESSUS_AGENT = agent-based scans)
      - last_seen within the past 90 days (full sync) or since_ts (delta)
    """
    base = tenant["url"].rstrip("/")

    # Valid export API filter fields (Tenable docs): has_plugin_results,
    # created_at, updated_at, first_seen, last_scan_time, terminated_at.
    # "last_seen" is NOT a valid export filter — we filter that client-side.
    flt: dict = {
        # Restrict to assets with Nessus/agent plugin results (VM module).
        "has_plugin_results": True,
    }
    if since_ts:
        # Delta: only assets updated since the last sync timestamp
        flt["updated_at"] = int(since_ts)

    payload = {
        "chunk_size":        1000,
        "include_unlicensed": False,
        "filters":            flt,
    }
    log.info(f"Export filters: {flt}")
    result = http_post(f"{base}/assets/export", payload,
                       headers=tenable_headers(tenant), timeout=30)
    export_id = result.get("export_uuid") or result.get("uuid") or result.get("id")
    if not export_id:
        raise RuntimeError(f"No export UUID in response: {result}")
    return export_id


def _wait_for_export(tenant: dict, export_id: str,
                     job_id: str | None = None) -> list[int]:
    """Poll GET /assets/export/{uuid}/status until FINISHED. Returns chunk list."""
    base = tenant["url"].rstrip("/")
    hdrs = tenable_headers(tenant)
    for attempt in range(EXPORT_POLL_MAX):
        time.sleep(EXPORT_POLL_INTERVAL)
        status = http_get(f"{base}/assets/export/{export_id}/status",
                          headers=hdrs, timeout=15)
        state  = status.get("status", "").upper()
        chunks = status.get("chunks_available", [])
        log.info(f"Export {export_id}: {state}, {len(chunks)} chunk(s) available")
        if job_id:
            _job_update(job_id, phase=f"Waiting for Tenable export… ({state})")
        if state == "FINISHED":
            return chunks
        if state in ("ERROR", "CANCELLED"):
            raise RuntimeError(f"Export {export_id} ended with status {state}")
    raise RuntimeError(f"Export {export_id} did not finish within timeout")


def _download_chunk(tenant: dict, export_id: str, chunk_id: int) -> list:
    """Download one export chunk and return the list of asset dicts."""
    base = tenant["url"].rstrip("/")
    return http_get(
        f"{base}/assets/export/{export_id}/chunks/{chunk_id}",
        headers=tenable_headers(tenant), timeout=60
    )


def export_assets(tenant: dict, since_ts: float | None = None,
                  job_id: str | None = None) -> list:
    """
    Full or delta asset export via POST /assets/export.
    - since_ts=None  → full export (all assets)
    - since_ts=float → delta export (only assets updated_at >= since_ts)
    Downloads all chunks in parallel and returns a flat list.
    """
    mode_label = f"since {datetime.fromtimestamp(since_ts, tz=timezone.utc).date()}" \
                 if since_ts else "full"
    log.info(f"Starting asset export ({mode_label})…")
    if job_id:
        _job_update(job_id, phase=f"Requesting export from Tenable ({mode_label})…")

    export_id = _start_export(tenant, since_ts)
    log.info(f"Export UUID: {export_id}")

    chunks = _wait_for_export(tenant, export_id, job_id=job_id)
    if not chunks:
        return []

    log.info(f"Downloading {len(chunks)} chunk(s) in parallel…")
    if job_id:
        _job_update(job_id, phase=f"Downloading {len(chunks)} chunk(s) in parallel…",
                    progress=0, total=len(chunks))

    assets: list = []
    lock   = threading.Lock()
    done   = [0]

    def fetch_chunk(cid):
        data = _download_chunk(tenant, export_id, cid)
        with lock:
            assets.extend(data)
            done[0] += 1
            if job_id:
                _job_update(job_id, progress=done[0], total=len(chunks),
                            phase=f"Downloading chunks… ({done[0]}/{len(chunks)})")
        return len(data)

    with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as ex:
        futs = {ex.submit(fetch_chunk, cid): cid for cid in chunks}
        for f in as_completed(futs):
            f.result()  # re-raise any exception

    log.info(f"Export complete: {len(assets)} raw assets downloaded")

    # ── Client-side 90-day last_seen filter ──────────────────────────────
    # The export API doesn't support a last_seen filter directly.
    # Drop assets not seen in the past 90 days to keep the DB lean.
    cutoff_iso = datetime.fromtimestamp(
        time.time() - 90 * 86400, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S")
    before = len(assets)
    assets = [
        a for a in assets
        if (a.get("last_seen") or "") >= cutoff_iso
    ]
    if before != len(assets):
        log.info(f"Filtered to {len(assets)} assets seen within 90 days "
                 f"(dropped {before - len(assets)} stale)")

    return assets


def _cpe_human_name(cpe_str: str) -> str:
    """Return a readable label for a CPE string."""
    try:
        if cpe_str.startswith("cpe:2.3:"):
            parts = cpe_str[8:].split(":")
            vendor, product, ver = parts[1], parts[2], parts[3]
        else:
            parts = cpe_str[5:].split(":")
            vendor, product = parts[1], parts[2]
            ver = parts[3].split("/")[0] if len(parts) > 3 else "*"
        name = product.replace("_", " ").title()
        if ver not in ("*", "-", ""):
            name += f" {ver}"
        return name
    except Exception:
        return cpe_str


_tag_debug_logged = False   # log raw tags once per process start

def build_asset(raw: dict, tenant_id: str) -> dict:
    """Build an enriched asset record from an export API asset object."""
    global _tag_debug_logged
    if not _tag_debug_logged:
        top_keys  = list(raw.keys())
        tags_raw_ = raw.get("tags")
        log.info(f"[tag-debug] First export asset keys: {top_keys}")
        log.info(f"[tag-debug] First export asset 'tags' field type={type(tags_raw_).__name__!r} value={tags_raw_!r}")
        _tag_debug_logged = True

    asset_id     = raw.get("id", "")
    hostnames    = raw.get("hostnames", []) or []
    fqdns        = raw.get("fqdns", []) or []
    ips          = raw.get("ipv4s", []) or []
    ipv6s        = raw.get("ipv6s", []) or []
    mac_addrs    = raw.get("mac_addresses", []) or []
    netbios      = raw.get("netbios_names", []) or []
    os_list      = raw.get("operating_systems", []) or []
    system_types = raw.get("system_types", []) or []
    # installed_software contains CPE strings (cpe:/a:... or cpe:2.3:...)
    software     = raw.get("installed_software", []) or []

    # Best display name: prefer FQDN → hostname → netbios → IP → UUID prefix
    name = (fqdns + hostnames + netbios + ips + [asset_id[:8] or "unknown"])[0]

    # ── EOL entries ─────────────────────────────────────────────────────────
    entries = []

    # OS-based EOL (highest fidelity)
    for os_str in os_list:
        info = parse_os_eol(os_str)
        if info is None:
            continue   # unmatched OS string — parse_os_eol returns None for these
        info["name"] = os_str
        info["type"] = "Operating System"
        entries.append(info)

    # Application CPE EOL from installed_software – include ALL, not just mapped ones
    seen_cpes: set = set()
    for cpe_str in software:
        if cpe_str in seen_cpes:
            continue
        seen_cpes.add(cpe_str)
        # Skip OS CPEs – already covered by operating_systems field
        try:
            prefix = cpe_str[8:] if cpe_str.startswith("cpe:2.3:") else cpe_str[5:]
            if prefix.startswith("o:"):
                continue
        except Exception:
            continue
        info = parse_cpe_eol(cpe_str)
        if info:
            info["name"]    = _cpe_human_name(cpe_str)
            info["cpe_raw"] = cpe_str
            info["type"]    = "Application"
            entries.append(info)
        else:
            # CPE not in our map – not tracked, skip silently
            # (visible in asset detail panel via raw CPE list)
            log.debug(f"Unmapped CPE skipped: {cpe_str}")

    # Sort: OS entries first (pinned), then apps by status urgency
    # Display priority: EOL(0) → EOL Soon(1) → Supported(2) → Unknown(3)
    DISPLAY_PRI = {"eol": 0, "eol_soon": 1, "supported": 2, "unknown": 3}
    os_entries  = [e for e in entries if e.get("type") == "Operating System"]
    app_entries = [e for e in entries if e.get("type") != "Operating System"]
    app_entries.sort(key=lambda x: (DISPLAY_PRI.get(x["status"], 3), (x.get("name") or "").lower()))
    entries = os_entries + app_entries

    # Overall worst status: eol > eol_soon > supported > unknown
    # "supported" beats "unknown" so a known-good OS isn't dragged down by unassessable apps
    RISK_PRI = {"eol": 0, "eol_soon": 1, "supported": 2, "unknown": 3}
    worst = min(entries, key=lambda x: RISK_PRI.get(x["status"], 2)) if entries else None

    # ── Tags (filterable) ────────────────────────────────────────────────────
    # Tenable export returns tags as [{uuid, key, value, added_by, added_at}].
    # "key" = category name (e.g. "OS Lifecycle"), "value" = tag value.
    tags_raw = raw.get("tags") or []
    tags     = [
        {"category": t.get("key", ""), "value": t.get("value", "")}
        for t in tags_raw
        if t.get("key") and t.get("value")
    ]

    # ── Extra attributes for drill-down ─────────────────────────────────────
    source_names = [s.get("name", "") for s in (raw.get("sources") or []) if s.get("name")]
    attributes   = {
        "fqdns":           fqdns,
        "ipv6s":           ipv6s[:5],
        "mac_addresses":   mac_addrs,
        "netbios_names":   netbios,
        "system_types":    system_types,
        "network_name":    raw.get("network_name"),
        "agent_uuid":      raw.get("agent_uuid"),
        "has_agent":       raw.get("has_agent", False),
        "first_seen":      raw.get("first_seen"),
        "last_scan_time":  raw.get("last_scan_time") or raw.get("last_authenticated_scan_date"),
        "sources":         source_names,
        "acr_score":       raw.get("acr_score"),
        "aes_score":       raw.get("exposure_score"),
        "tags":            tags,     # mirrored here for drill-down display
        "bios_uuid":       raw.get("bios_uuid"),
    }

    return {
        "id":             asset_id,
        "tenant_id":      tenant_id,
        "name":           name,
        "ips":            ips[:5],
        "hostnames":      (hostnames + fqdns)[:5],
        "os":             os_list[0] if os_list else None,
        "last_seen":      raw.get("last_seen", ""),
        "overall_status": worst["status"] if worst else "unknown",
        "eol_entries":    entries,
        "software":       software,       # raw CPE strings for display
        "attributes":     attributes,
        "tags":           tags,           # top-level for DB storage and filtering
    }

# ── Background sync job ───────────────────────────────────────────────────────

def _job_update(job_id: str, **kw):
    with _jobs_lock:
        _jobs[job_id].update(kw)


def _run_sync_job(job_id: str, tenant_id: str, mode: str):
    cfg    = load_config()
    tenant = next((t for t in cfg["tenants"] if t["id"] == tenant_id), None)
    if not tenant:
        _job_update(job_id, status="error", error="Tenant not found"); return

    try:
        # ── Step 0: refresh EOL reference data ───────────────────────────────
        _job_update(job_id, status="warming", phase="Refreshing EOL reference data…")
        refresh_eol_cache(force=True)

        # ── Step 1: export all assets from Tenable ───────────────────────────
        _job_update(job_id, status="fetching", phase="Starting asset export…")
        raws = export_assets(tenant, since_ts=None, job_id=job_id)

        total = len(raws)
        log.info(f"[{job_id}] {total} assets exported from Tenable ({mode})")
        _job_update(job_id, status="warming", phase="Loading EOL reference data…",
                    fetched=total, progress=0, total=total)

        # ── Step 2: pre-warm EOL cache in parallel ───────────────────────────
        needed = set()
        for raw in raws:
            for os_str in (raw.get("operating_systems") or []):
                p, _ = _os_product_version(os_str)
                if p: needed.add(p)
            # Also pre-warm CPE-mapped products from installed_software
            for cpe_str in (raw.get("installed_software") or []):
                try:
                    if cpe_str.startswith("cpe:2.3:"):
                        parts = cpe_str[8:].split(":")
                        if len(parts) >= 3:
                            key = f"{parts[1]}:{parts[2]}"
                    else:
                        parts = cpe_str[5:].split(":")
                        if len(parts) >= 3:
                            key = f"{parts[1]}:{parts[2]}"
                        else: continue
                    if key in CPE_MAP:
                        needed.add(CPE_MAP[key][0])
                except Exception:
                    pass
        prewarm_eol(needed)

        # ── Step 3: build enriched asset records ─────────────────────────────
        _job_update(job_id, status="processing", phase="Processing assets…")
        assets = []
        for i, raw in enumerate(raws, 1):
            assets.append(build_asset(raw, tenant_id))
            if i % 100 == 0 or i == total:
                _job_update(job_id, progress=i)

        # ── Step 4: persist to SQLite ─────────────────────────────────────────
        _job_update(job_id, status="saving", phase="Saving to local database…")
        # Ensure overall_status is consistent with eol_entries before saving
        for a in assets:
            a["overall_status"] = _recompute_overall_status(a.get("eol_entries", []))
        final_count = db_upsert_assets(tenant_id, assets, replace_all=True)
        db_update_sync_state(tenant_id, "full", final_count)

        _job_update(job_id,
                    status="done",
                    total_in_db=final_count,
                    fetched=total,
                    mode="full",
                    phase="Complete")
        log.info(f"[{job_id}] Done – DB now has {final_count} assets")

    except Exception as e:
        log.error(f"[{job_id}] Error: {e}")
        _job_update(job_id, status="error", error=str(e))


def start_job(tenant_id: str, mode: str) -> str:
    job_id = str(uuid.uuid4())[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "starting", "progress": 0, "total": 0,
                         "tenant_id": tenant_id, "mode": mode, "phase": "Starting…"}
    threading.Thread(target=_run_sync_job, args=(job_id, tenant_id, mode),
                     daemon=True).start()
    return job_id

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(f"{self.address_string()} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self):
        with open(TEMPLATE, "rb") as f: body = f.read()
        self.send_response(200)
        self.send_header("Content-Type",  "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/":
            self.send_html(); return

        # Tenant list
        if path == "/api/tenants":
            cfg    = load_config()
            masked = [{"id": t["id"], "name": t["name"], "url": t["url"],
                       "access_key": t["access_key"][:6]+"****" if t.get("access_key") else "",
                       "has_secret": bool(t.get("secret_key"))}
                      for t in cfg["tenants"]]
            self.send_json(masked); return

        # Test tenant connectivity
        m = re.match(r"^/api/tenants/([^/]+)/test$", path)
        if m:
            t = next((x for x in load_config()["tenants"] if x["id"] == m.group(1)), None)
            if not t:
                self.send_json({"ok": False, "error": "Not found"}, 404); return
            try:
                data = http_get(f"{t['url'].rstrip('/')}/server/status",
                                headers=tenable_headers(t), timeout=10)
                self.send_json({"ok": True, "status": data})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # Job status
        m = re.match(r"^/api/jobs/([^/]+)$", path)
        if m:
            with _jobs_lock:
                job = dict(_jobs.get(m.group(1), {}))
            if not job:
                self.send_json({"error": "Job not found"}, 404); return
            self.send_json(job); return

        # Assets – served instantly from SQLite
        if path == "/api/assets":
            tenant_id = (qs.get("tenant") or [""])[0]
            if not tenant_id:
                self.send_json({"error": "tenant param required"}, 400); return
            assets     = db_get_assets(tenant_id)
            sync_state = db_get_sync_state(tenant_id)
            self.send_json({
                "success":    True,
                "assets":     assets,
                "count":      len(assets),
                "sync_state": sync_state,
                "from_db":    True,
            }); return

        # Sync state only (lightweight)
        if path == "/api/sync-state":
            tenant_id = (qs.get("tenant") or [""])[0]
            if not tenant_id:
                self.send_json({"error": "tenant param required"}, 400); return
            self.send_json(db_get_sync_state(tenant_id)); return

        # Summary across all tenants
        if path == "/api/summary":
            cfg    = load_config()
            totals = {"eol": 0, "eol_soon": 0, "supported": 0, "unknown": 0, "total": 0}
            out    = []
            for t in cfg["tenants"]:
                tid = t["id"]
                with get_conn() as con:
                    rows = con.execute(
                        "SELECT overall_status, COUNT(*) as n FROM assets "
                        "WHERE tenant_id=? GROUP BY overall_status", (tid,)
                    ).fetchall()
                counts = {r["overall_status"]: r["n"] for r in rows}
                counts["total"] = sum(counts.values())
                ss = db_get_sync_state(tid)
                out.append({"id": tid, "name": t["name"], **counts,
                            "last_full_sync": ss.get("last_full_sync"),
                            "last_delta_sync": ss.get("last_delta_sync")})
                for k in ["eol", "eol_soon", "supported", "unknown"]:
                    totals[k] += counts.get(k, 0)
                totals["total"] += counts["total"]
            self.send_json({"tenants": out, "totals": totals}); return

        # Asset tags – unique {category, value} pairs for filter dropdown
        if path == "/api/tags":
            tenant_id = (qs.get("tenant") or [""])[0]
            if not tenant_id:
                self.send_json({"error": "tenant param required"}, 400); return
            with get_conn() as con:
                rows = con.execute(
                    "SELECT tags FROM assets WHERE tenant_id=? AND tags IS NOT NULL AND tags != '[]'",
                    (tenant_id,)
                ).fetchall()
            seen: dict = {}
            for row in rows:
                for t in json.loads(row["tags"] or "[]"):
                    cat = t.get("category", "")
                    val = t.get("value", "")
                    if cat and val:
                        key = f"{cat}:{val}"
                        if key not in seen:
                            seen[key] = {"category": cat, "value": val}
            result = sorted(seen.values(), key=lambda x: (x["category"], x["value"]))
            self.send_json(result); return

        # ── Debug: inspect CPE strings and their parse results ────────────────
        # GET /api/debug/cpe?q=nginx&tenant=<id>
        # Returns: matched CPE strings + parse_cpe_eol result for each.
        # Useful for diagnosing why a product isn't matching.
        if path == "/api/debug/cpe":
            q         = (qs.get("q") or [""])[0].lower()
            tenant_id = (qs.get("tenant") or [""])[0]
            if not q:
                self.send_json({"error": "q param required (e.g. ?q=nginx)"}, 400); return
            # Collect all CPE strings from assets (across all tenants or filtered)
            with get_conn() as con:
                sql = "SELECT id, name, software FROM assets"
                args: tuple = ()
                if tenant_id:
                    sql  += " WHERE tenant_id=?"
                    args  = (tenant_id,)
                rows = con.execute(sql, args).fetchall()
            results = []
            for row in rows:
                cpes = json.loads(row["software"] or "[]")
                for cpe in cpes:
                    if q in cpe.lower():
                        parsed = parse_cpe_eol(cpe)
                        # Also show what key was looked up
                        vendor = product_name = ver_raw = "?"
                        try:
                            if cpe.startswith("cpe:2.3:"):
                                p = cpe[8:].split(":")
                                vendor, product_name, ver_raw = p[1], p[2], p[3]
                            elif cpe.startswith("cpe:/"):
                                p = cpe[5:].split(":")
                                vendor, product_name = p[1], p[2]
                                ver_raw = p[3].split("/")[0] if len(p) > 3 else "*"
                        except Exception:
                            pass
                        key = f"{vendor}:{product_name}"
                        results.append({
                            "asset_id":    row["id"],
                            "asset_name":  row["name"],
                            "cpe":         cpe,
                            "cpe_key":     key,
                            "key_in_map":  key in CPE_MAP,
                            "ver_raw":     ver_raw,
                            "parse_result": parsed,
                        })
            # Also report EOL cache state for products that matched
            cache_info = {}
            with EOL_LOCK:
                for r in results:
                    if r["parse_result"] and r["parse_result"].get("product"):
                        p = r["parse_result"]["product"]
                        if p not in cache_info:
                            cycles = EOL_CACHE.get(p, [])
                            ts     = EOL_CACHE_TS.get(p, 0)
                            cache_info[p] = {
                                "cycle_count": len(cycles),
                                "cached_at":   ts,
                                "age_hours":   round((time.time() - ts) / 3600, 1) if ts else None,
                            }
            self.send_json({
                "query":      q,
                "tenant":     tenant_id or "all",
                "cpe_matches": results,
                "eol_cache":   cache_info,
            }); return

        # ── Debug: full CPE breakdown for a specific asset ────────────────────
        # GET /api/debug/asset-cpes?name=beehive&tenant=<id>
        # OR  /api/debug/asset-cpes?id=<uuid>&tenant=<id>
        # Shows every CPE string for the asset and why it is/isn't in EOL analysis.
        if path == "/api/debug/asset-cpes":
            tenant_id  = (qs.get("tenant") or [""])[0]
            asset_name = (qs.get("name")   or [""])[0].lower()
            asset_id_q = (qs.get("id")     or [""])[0]
            if not tenant_id:
                self.send_json({"error": "tenant param required"}, 400); return
            with get_conn() as con:
                if asset_id_q:
                    row = con.execute(
                        "SELECT id, name, software, attributes, eol_entries FROM assets "
                        "WHERE tenant_id=? AND id=? LIMIT 1", (tenant_id, asset_id_q)
                    ).fetchone()
                else:
                    row = con.execute(
                        "SELECT id, name, software, attributes, eol_entries FROM assets "
                        "WHERE tenant_id=? AND lower(name) LIKE ? LIMIT 1",
                        (tenant_id, f"%{asset_name}%")
                    ).fetchone()
            if not row:
                self.send_json({"error": "asset not found"}, 404); return

            cpes       = json.loads(row["software"]    or "[]")
            eol_entries= json.loads(row["eol_entries"] or "[]")
            analyzed_names = {e.get("name", "") for e in eol_entries}

            breakdown = {"analyzed": [], "mapped_no_cycle": [], "not_in_map": []}
            idx = _get_cpe_product_index()

            for cpe in cpes:
                entry = {"cpe": cpe}
                # Parse vendor:product:version from CPE string
                try:
                    if cpe.startswith("cpe:2.3:"):
                        parts = cpe[8:].split(":")
                        entry["part"]    = parts[0]
                        entry["vendor"]  = parts[1]
                        entry["product"] = parts[2]
                        entry["version"] = parts[3]
                    elif cpe.startswith("cpe:/"):
                        parts = cpe[5:].split(":")
                        entry["part"]    = parts[0]
                        entry["vendor"]  = parts[1] if len(parts) > 1 else ""
                        entry["product"] = parts[2] if len(parts) > 2 else ""
                        entry["version"] = parts[3].split("/")[0] if len(parts) > 3 else "*"
                except Exception:
                    pass

                part = entry.get("part", "")
                if part == "o":
                    # OS CPEs are handled via OS string parsing, not CPE_MAP
                    entry["note"] = "OS CPE — handled via OS string, not CPE_MAP"
                    breakdown.setdefault("os_cpe", []).append(entry)
                    continue

                parsed = parse_cpe_eol(cpe)
                if parsed:
                    entry["eol_product"] = parsed.get("product")
                    entry["version_matched"] = parsed.get("cycle")
                    entry["status"] = parsed.get("status")
                    entry["in_eol_analysis"] = parsed.get("name") in analyzed_names or any(
                        e.get("product") == parsed.get("product") for e in eol_entries
                    )
                    breakdown["analyzed"].append(entry)
                else:
                    # Check if vendor:product is in map (parse_cpe_eol returned None)
                    key = f"{entry.get('vendor','')}:{entry.get('product','')}"
                    if key in CPE_MAP:
                        slug, pattern = CPE_MAP[key]
                        ver = entry.get("version", "?")
                        m = re.search(pattern, ver)
                        if m:
                            entry["note"] = (
                                f"In CPE_MAP → {slug}, regex matched '{m.group(1)}' "
                                f"but suppressed by product normalizer "
                                f"(e.g. M365 Apps rolling release, bad DLL version)"
                            )
                        else:
                            entry["note"] = f"In CPE_MAP → {slug}, but version '{ver}' didn't match regex {pattern!r}"
                        breakdown["mapped_no_cycle"].append(entry)
                    elif entry.get("product") in idx:
                        entry["note"] = f"Found via product-only fallback → {idx[entry['product']][0]}, but no cycle matched"
                        breakdown["mapped_no_cycle"].append(entry)
                    else:
                        entry["note"] = f"vendor:product key '{key}' not in CPE_MAP — no endoflife.date mapping"
                        breakdown["not_in_map"].append(entry)

            self.send_json({
                "asset_id":    row["id"],
                "asset_name":  row["name"],
                "total_cpes":  len(cpes),
                "eol_entries": len(eol_entries),
                "breakdown":   breakdown,
                "summary": {
                    "analyzed":       len(breakdown.get("analyzed", [])),
                    "mapped_no_cycle": len(breakdown.get("mapped_no_cycle", [])),
                    "not_in_map":     len(breakdown.get("not_in_map", [])),
                    "os_cpes":        len(breakdown.get("os_cpe", [])),
                }
            }); return

        # ── Software search ───────────────────────────────────────────────────
        # GET /api/software/search?q=<query>&tenant=<id>[&tag=<cat>:<val>]
        # Returns all CPE matches grouped by (vendor, product, version) with EOL status.
        if path == "/api/software/search":
            tenant_id = (qs.get("tenant") or [""])[0]
            query     = (qs.get("q")      or [""])[0].strip().lower()
            tag_filter = (qs.get("tag")   or [""])[0]   # "Category:Value"
            if not tenant_id or len(query) < 2:
                self.send_json({"error": "tenant and q (min 2 chars) required"}, 400); return

            with sqlite3.connect(DB_FILE) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT id, name, ips, software, tags FROM assets WHERE tenant_id=? AND software IS NOT NULL AND software != '[]'",
                    (tenant_id,)
                ).fetchall()

            # Optionally restrict to assets matching a tag
            if tag_filter and ":" in tag_filter:
                tcat, tval = tag_filter.split(":", 1)
                def _has_tag(row):
                    for t in json.loads(row["tags"] or "[]"):
                        if (t.get("category") or t.get("key", "")) == tcat and t.get("value") == tval:
                            return True
                    return False
                rows = [r for r in rows if _has_tag(r)]

            # CPE format: cpe:/a:vendor:product:version  or  cpe:2.3:a:vendor:product:version
            _CPE_RE = re.compile(r"^cpe:[/\d.]*[:/](?P<part>[aoh]):(?P<vendor>[^:]+):(?P<product>[^:]+):?(?P<version>[^:]*)")

            # Group by (vendor, product, version) → {assets, eol info, ...}
            groups: dict = {}

            for row in rows:
                cpes = json.loads(row["software"] or "[]")
                ips  = json.loads(row["ips"] or "[]")
                asset_ip = ips[0] if ips else ""
                asset_info = {"id": row["id"], "name": row["name"], "ip": asset_ip}

                seen_in_asset: set = set()   # dedup (vendor, product, version) per asset

                for cpe in cpes:
                    m = _CPE_RE.match(cpe)
                    if not m:
                        continue
                    vendor  = m.group("vendor").lower()
                    product = m.group("product").lower()
                    version = m.group("version") or "*"

                    # Skip OS CPEs — those are handled separately in EOL analysis
                    if m.group("part") == "o":
                        continue

                    # Query matching: search in vendor and product fields
                    if query not in vendor and query not in product:
                        continue

                    key = (vendor, product, version)
                    if key in seen_in_asset:
                        continue
                    seen_in_asset.add(key)

                    if key not in groups:
                        # Get EOL status for this CPE
                        parsed = parse_cpe_eol(cpe)
                        groups[key] = {
                            "vendor":       vendor,
                            "product":      product,
                            "version":      version,
                            "eol_product":  parsed.get("product")        if parsed else None,
                            "eol_cycle":    parsed.get("cycle")          if parsed else None,
                            "eol_status":   parsed.get("status", "not_tracked") if parsed else "not_tracked",
                            "eol_date":     parsed.get("eol_date")       if parsed else None,
                            "days_remaining": parsed.get("days_remaining") if parsed else None,
                            "eol_url":      parsed.get("url")            if parsed else None,
                            "assets":       [],
                        }

                    if not any(a["id"] == asset_info["id"] for a in groups[key]["assets"]):
                        groups[key]["assets"].append(asset_info)

            # Sort: eol first, then eol_soon, not_tracked/unknown, supported; then product, then version desc
            STATUS_PRI = {"eol": 0, "eol_soon": 1, "not_tracked": 2, "unknown": 3, "supported": 4}
            result_list = sorted(
                groups.values(),
                key=lambda g: (STATUS_PRI.get(g["eol_status"], 3), g["product"], g["version"])
            )

            # Add asset_count for convenience
            for g in result_list:
                g["asset_count"] = len(g["assets"])

            total_assets = len({a["id"] for g in result_list for a in g["assets"]})
            self.send_json({
                "query":          query,
                "total_versions": len(result_list),
                "total_assets":   total_assets,
                "groups":         result_list,
            }); return

        # ── Software inventory (pre-aggregated, backend-computed) ────────────
        # GET /api/software/inventory?tenant=<id>[&q=<filter>][&tag=<cat:val>]
        # Groups all CPEs by (vendor, product) → versions → asset_ids.
        # Returns asset_ids (not full objects) to keep payload small;
        # the frontend resolves names from its already-loaded allAssets map.
        if path == "/api/software/inventory":
            tenant_id  = (qs.get("tenant") or [""])[0]
            query      = (qs.get("q")      or [""])[0].strip().lower()
            tag_filter = (qs.get("tag")    or [""])[0]
            if not tenant_id:
                self.send_json({"error": "tenant required"}, 400); return

            with sqlite3.connect(DB_FILE) as con:
                con.row_factory = sqlite3.Row
                db_rows = con.execute(
                    "SELECT id, name, ips, software, tags FROM assets WHERE tenant_id=? AND software IS NOT NULL AND software != '[]'",
                    (tenant_id,)
                ).fetchall()

            # Tag pre-filter
            if tag_filter and ":" in tag_filter:
                tcat, tval = tag_filter.split(":", 1)
                def _inv_has_tag(row):
                    for t in json.loads(row["tags"] or "[]"):
                        if (t.get("category") or t.get("key", "")) == tcat and t.get("value") == tval:
                            return True
                    return False
                db_rows = [r for r in db_rows if _inv_has_tag(r)]

            _CPE_RE2 = re.compile(
                r"^cpe:[/\d.]*[:/](?P<part>[aoh]):(?P<vendor>[^:]+):(?P<product>[^:]+):?(?P<version>[^:]*)"
            )
            STATUS_PRI2 = {"eol": 0, "eol_soon": 1, "not_tracked": 2, "unknown": 3, "supported": 4}

            # Two-level grouping: pkey("vendor:product") → version → asset_ids
            products: dict = {}   # pkey → {vendor, product, worst_pri, worst_status, versions}

            # eol_cache_local: (vendor, product, version) → parsed result
            eol_cache_local: dict = {}

            for row in db_rows:
                cpes = json.loads(row["software"] or "[]")
                asset_id = row["id"]
                seen: set = set()

                for cpe in cpes:
                    m = _CPE_RE2.match(cpe)
                    if not m or m.group("part") == "o":
                        continue
                    vendor  = m.group("vendor").lower()
                    product = m.group("product").lower()
                    version = m.group("version") or "*"

                    # Optionally filter by query string
                    if query and query not in vendor and query not in product:
                        continue

                    vkey = (vendor, product, version)
                    if vkey in seen:
                        continue
                    seen.add(vkey)

                    pkey = f"{vendor}:{product}"
                    if pkey not in products:
                        products[pkey] = {
                            "vendor": vendor, "product": product,
                            "worst_pri": 4, "worst_status": "supported",
                            "versions": {}
                        }
                    p = products[pkey]

                    if version not in p["versions"]:
                        if vkey not in eol_cache_local:
                            parsed = parse_cpe_eol(cpe)
                            eol_cache_local[vkey] = parsed
                        else:
                            parsed = eol_cache_local[vkey]
                        st = parsed.get("status", "not_tracked") if parsed else "not_tracked"
                        p["versions"][version] = {
                            "version":        version,
                            "eol_status":     st,
                            "eol_cycle":      parsed.get("cycle")          if parsed else None,
                            "eol_date":       parsed.get("eol_date")       if parsed else None,
                            "days_remaining": parsed.get("days_remaining") if parsed else None,
                            "eol_url":        parsed.get("url")            if parsed else None,
                            "asset_ids":      [],
                        }
                        pri = STATUS_PRI2.get(st, 3)
                        if pri < p["worst_pri"]:
                            p["worst_pri"] = pri; p["worst_status"] = st

                    p["versions"][version]["asset_ids"].append(asset_id)

            # Assemble and sort
            product_list = []
            for p in products.values():
                versions_sorted = sorted(
                    p["versions"].values(),
                    key=lambda v: (STATUS_PRI2.get(v["eol_status"], 3), v["version"])
                )
                for v in versions_sorted:
                    v["asset_count"] = len(v["asset_ids"])
                product_list.append({
                    "vendor":        p["vendor"],
                    "product":       p["product"],
                    "worst_status":  p["worst_status"],
                    "total_assets":  len({aid for v in p["versions"].values() for aid in v["asset_ids"]}),
                    "version_count": len(p["versions"]),
                    "versions":      versions_sorted,
                })
            product_list.sort(key=lambda p: (STATUS_PRI2.get(p["worst_status"], 3), p["product"]))

            self.send_json({
                "total_products": len(product_list),
                "products":       product_list,
            }); return

        # ── Debug: inspect EOL cache for a specific product ──────────────────
        # GET /api/debug/eol-cache?product=curl
        # Shows the raw cycle list stored in memory for the product.
        if path == "/api/debug/eol-cache":
            product = (qs.get("product") or [""])[0]
            if not product:
                self.send_json({"error": "product param required"}, 400); return
            with EOL_LOCK:
                cycles = list(EOL_CACHE.get(product, []))
                ts     = EOL_CACHE_TS.get(product, 0)
            self.send_json({
                "product":     product,
                "cycle_count": len(cycles),
                "fetched_at":  ts,
                "age_hours":   round((time.time() - ts) / 3600, 1) if ts else None,
                "cycles":      cycles[:20],   # first 20 to avoid huge response
            }); return

        # ── Debug: live tag API check ─────────────────────────────────────────
        # GET /api/debug/tags-check?tenant=<id>
        # Calls Tenable's Tags API directly to verify tag format and access.
        # Safe read-only endpoint — does not modify any data.
        if path == "/api/debug/tags-check":
            tenant_id = (qs.get("tenant") or [""])[0]
            if not tenant_id:
                self.send_json({"error": "tenant param required"}, 400); return
            cfg    = load_config()
            tenant = next((t for t in cfg["tenants"] if t["id"] == tenant_id), None)
            if not tenant:
                self.send_json({"error": "tenant not found"}, 404); return
            base   = tenant["url"].rstrip("/")
            hdrs   = tenable_headers(tenant)
            result = {"tenant_id": tenant_id}
            # 1. List all tag values (GET /tags/values)
            try:
                tv = http_get(f"{base}/tags/values?limit=10", headers=hdrs, timeout=15)
                result["tags_values_sample"] = tv
            except Exception as e:
                result["tags_values_error"] = str(e)
            # 2. Get a known asset UUID from the DB and call GET /tags/assets/{uuid}/assignments
            try:
                with get_conn() as con:
                    row = con.execute(
                        "SELECT id FROM assets WHERE tenant_id=? LIMIT 1", (tenant_id,)
                    ).fetchone()
                if row:
                    asset_uuid = row["id"]
                    result["sample_asset_uuid"] = asset_uuid
                    ta = http_get(f"{base}/tags/assets/{asset_uuid}/assignments",
                                  headers=hdrs, timeout=15)
                    result["asset_tags_raw"] = ta
                else:
                    result["asset_tags_raw"] = "no assets in DB"
            except Exception as e:
                result["asset_tags_error"] = str(e)
            self.send_json(result); return

        self.send_error(404)

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path

        # Start sync job
        if path == "/api/jobs":
            body      = self.read_body()
            tenant_id = body.get("tenant_id", "")
            if not tenant_id:
                self.send_json({"error": "tenant_id required"}, 400); return
            job_id = start_job(tenant_id, "full")
            self.send_json({"job_id": job_id, "mode": "full"}, 202); return

        # Add tenant
        if path == "/api/tenants":
            body = self.read_body()
            if not all(body.get(k) for k in ["name", "url", "access_key", "secret_key"]):
                self.send_json({"error": "Required: name, url, access_key, secret_key"}, 400); return
            cfg    = load_config()
            tenant = {"id": str(uuid.uuid4())[:8], "name": body["name"].strip(),
                      "url": body["url"].rstrip("/"),
                      "access_key": body["access_key"].strip(),
                      "secret_key": body["secret_key"].strip()}
            cfg["tenants"].append(tenant)
            save_config(cfg)
            self.send_json({"id": tenant["id"], "name": tenant["name"]}, 201); return

        self.send_error(404)

    # ── PUT ───────────────────────────────────────────────────────────────────
    def do_PUT(self):
        m = re.match(r"^/api/tenants/([^/]+)$", urlparse(self.path).path)
        if m:
            tid  = m.group(1)
            body = self.read_body()
            cfg  = load_config()
            for t in cfg["tenants"]:
                if t["id"] == tid:
                    for k in ["name", "url", "access_key", "secret_key"]:
                        if body.get(k):
                            t[k] = body[k].strip().rstrip("/") if k=="url" else body[k].strip()
                    save_config(cfg)
                    self.send_json({"ok": True}); return
            self.send_json({"error": "Not found"}, 404); return
        self.send_error(404)

    # ── DELETE ────────────────────────────────────────────────────────────────
    def do_DELETE(self):
        m = re.match(r"^/api/tenants/([^/]+)$", urlparse(self.path).path)
        if m:
            tid = m.group(1)
            cfg = load_config()
            cfg["tenants"] = [t for t in cfg["tenants"] if t["id"] != tid]
            save_config(cfg)
            with get_conn() as con:
                con.execute("DELETE FROM assets    WHERE tenant_id=?", (tid,))
                con.execute("DELETE FROM sync_state WHERE tenant_id=?", (tid,))
            self.send_json({"ok": True}); return
        self.send_error(404)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    _load_eol_from_db()   # seed in-memory cache from persisted cycles
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"Tenable EOL Portal → http://localhost:{PORT}")
    log.info("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
