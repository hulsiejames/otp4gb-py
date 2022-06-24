# python-otp4gb

This is a port of the [OTP4GB][OTP4GB] tool written by Tom Forth at Open Innovations.

[OTP4GB]: https://github.com/odileeds/OTP4GB

## Preparation and dependencies

Make sure you have a working Java environment, and have Maven installed.

Run the setup script to install dependencies.

```
python setup.py
```

This will run maven and download the osmconvert64 executable for Windows.

You will also need to install a series of python libraries, which are captured in requirements files. There are two: one for conda and one for pip.

## Running

You will need to create a directory to set up the tool. This needs the following structure:

* `config.yml` A file with config for the run. A sample is provided in the root of this project.
* `input` directory containing the latest osm download
* `input\gtfs` directory containing the GTFS files that are to be included in the OTP routing graph

### `prepare.py`

This script prepares the OTP directory.

Usage:

```
prepare.py [-F] [-d <date>] [-b <bounds name>] <directory>
```

command line options

* `-b, --bounds` Specify bounds
* `-d, --date` Specify date for filtering
* `-F, --force` Force overwrite of existing files

Environment config

  `OSMCONVERT_DOCKER` - if set, will use a dockerised version of osmconvert (useful to run on macos, where there appears to be no native osmconvert)