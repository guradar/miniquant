#!/usr/bin/env python

import ConfigParser

config = ConfigParser.ConfigParser()
config.read("miniquant.ini")
sections=config.sections()
print "sections block:", sections
o = config.options("mysql")
print "section item:", o
v = config.items("mysql")
print "content:", v

dbhostname=config.get("mysql","hostname")
print dbhostname
