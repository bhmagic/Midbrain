# Archived legacy Skills

Packages in this directory are historical source snapshots. They are not
installed by workspace setup, scanned by agent discovery, built into active
artifacts, or included in current validation.

`vegetable_cutting` was archived during the spatial-frame convention V2
migration because its accumulated task-specific direction handling mixed
camera-optical, world, and arm-base coordinates. Its code is preserved only as
development evidence. Any replacement must be designed as smaller Skills that
consume the canonical spatial contract rather than copying this package.
