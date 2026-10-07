{
  "entryLayer": "messaging",
  "preset": "logos.dev",
  "messagingOverrides": {
    "plugin-kad-discovery": true,
    "kad-service-lookup-interval": @LOOKUP@,
    "discv5-discovery": false,
    "tcp-port": @TCP_PORT@,
    "nat": "extip:@IP@",
    "log-level": "DEBUG"
  }
}
