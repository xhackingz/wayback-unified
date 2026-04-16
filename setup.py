from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="wayback-unified",
    version="1.0.0",
    description="Maximum-coverage Wayback Machine URL harvester combining waymore, waybackurls, and waybackpy techniques",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="xhackingz",
    url="https://github.com/xhackingz/wayback-unified",
    py_modules=["wayback_unified"],
    python_requires=">=3.7",
    install_requires=[
        "urllib3>=1.26.0",
    ],
    entry_points={
        "console_scripts": [
            "wayback-unified=wayback_unified:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Security",
        "Topic :: Internet :: WWW/HTTP",
    ],
)
