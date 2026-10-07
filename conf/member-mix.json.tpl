{
  "entryLayer": "messaging",
  "messagingOverrides": {
    "cluster-id": @CLUSTER@,
    "num-shards-in-network": @SHARDS@,
    "plugin-kad-discovery": true,
    "anonymityLevel": "Preferred",
    "kad-bootstrap-node": [@BOOTSTRAPS@],
    "kad-service-lookup-interval": @LOOKUP@,
    "discv5-discovery": false,
    "tcp-port": @TCP_PORT@,
    "nat": "extip:@IP@",
    "log-level": "DEBUG"
  }
}
